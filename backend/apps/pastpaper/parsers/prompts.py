# =========================
# PromptBuilder（stub，复刻原 build_messages）
# =========================
import base64


class PromptBuilder:
    def __init__(self):
        self.base_context = ""

    def build(self, page_input, input_type, tree_json, last_node_json):
        if input_type == "text":
            user = f"""
        You are given OCR extracted text of a page from an exam paper (may contains inaccuracies because of noise)
        Page text (OCR extracted):
        \"\"\"{page_input}\"\"\"

        {self.base_context}
        """
            return [{"role": "system", "content": "You are an exam paper parser."},
                    {"role": "user", "content": user}], {"temperature": 0}
        elif input_type == "image":
            user = f"""
        You are given an image of a page from an exam paper.
        Read the visible text in the image and fulfill your task described below.
        If you encountered diagrams that are hard to express in text, ignore them. Otherwise, extract all text.

        Write using standard Unicode. For math, use plain-text formulas (x^2, 1/2).
        {self.base_context}
        """
            b64 = base64.b64encode(page_input).decode("utf-8")
            return [
                {"role": "system", "content": "You are an exam paper parser."},
                {"role": "user", "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                ]}
            ], {"temperature": 0, "reasoning_effort": "minimal"}
        else:
            raise ValueError(f"Unknown input_type: {input_type}")


class BaseQPPromptBuilder(PromptBuilder):
    def __init__(self):
        super().__init__()

    def build(self, page_input, input_type, tree_json, last_node_json):
        self.base_context = f"""
Current tree context (simplified):
{tree_json}

Last parsed node:
{last_node_json}

Instructions:
For each question-like block on this page, create an ARRAY of JSON objects that describe each questions.
Each objects is with this schema:
{{
  "num": string | null,
  "content": string,
  "marks": integer | null,
  "level": integer
}}
YOU MUST OUTPUT an ARRAY of valid JSON objects (not an JSON object that starts with {{}} but rather [{{...}},{{...}},]) WITH NO EXTRA FIELDS OR TEXT
i.e. The Json should be [{{"num": ..., "content": ..., "marks": ..., "level": ...}},{{"num": ..., "content": ..., "marks": ..., "level": ...}}, ...]

Rules:
1. "num": extract ONLY the local question number token exactly as it appears at the start 
   of the question line. 
   - Examples:
     - If the paper shows "1", output "1".
     - If the paper shows "(a)", output "(a)" (NOT "1(a)").
     - If the paper shows "(ii)", output "(ii)" (NOT "1(a)(ii)").
     - If the paper shows "Q3", output "Q3".
     - If the text is a continuation without a number, set "num": null.
   Do not combine child numbers with their parents.
   Special Case — Overlapping Numbers: When nested numbering appears (e.g., 1(a)(i)), ensure that:
      - The actual question content is stored under the smallest unit ((i) in this example).
      - Parent nodes (1, (a)) are created as empty structural nodes to preserve hierarchy, but they must not hold repeated text.
    For any parts that appear to be sub-question of a parent question but lack explicit numbering, make suitable numbering by your logics to make the hierarchy clear.
    If there is a Either...Or question, do not make them sub-question but instead the content of one question.
2. "content": extract the full question text (including background, description, and all body parts), 
   but always exclude any explicit marks notation. 
   e.g. Valid content includes the description of the background of the question that is necessary for candidates to know, like source materials that are essential for answering the question.

3. "marks": extract the mark allocation if present, as an integer. 
   - Normally it appears in square brackets [x], e.g. "[4]" → 4. 
   - Sometimes it may appear in other notations such as "(4 marks)". 
   - If such alternative formats are clearly indicating marks, normalize them to the integer (e.g., "(4 marks)" → 4).
   - If a single question appear to have multiple mark allocations (e.g., "[2] ... [2]"), sum them up.
   - If Either/Or, assign marks to the parent, children null.
4. "level": infer hierarchy using context. Continuations (num=null) inherit last_node.level.
5. Ignore headers/footers/admin text. Output only a valid JSON array containing the extracted question objects.

Return only an ARRAY of valid JSON objects (not a single JSON object that starts with {{}} but rather [{{...}},{{...}},]) of question objects — nothing else.
Each object must be separated by a comma and enclosed within square brackets.
Example of correct formatting (for illustration only):  
[
  {{"num": "1", "content": "Question text...", "marks": null, "level": 1}},
  {{"num": "(a)", "content": "Sub-question text...", "marks": 3, "level": 2}},
  {{"num": "(b)", "content": "Another question...", "marks": 4, "level": 2}}
]
"""
        return super().build(page_input, input_type, tree_json, last_node_json)

class BaseMSPromptBuilder(PromptBuilder):
    def __init__(self):
        super().__init__()

    def build(self, page_input, input_type, tree_json, last_node_json):
        self.base_context = f"""You are given the mark scheme of an exam paper page
Current tree context (simplified):
{tree_json}

Last parsed node:
{last_node_json}

Instructions:
For each question-like block on this page in the MARK SCHEME, create an object with this schema:
{{
  "num": string | null,
  "content": string,
  "marks": integer | null,
  "level": integer
}}
YOU MUST OUTPUT A VALID JSON ARRAY WITH NO EXTRA FIELDS OR TEXT
i.e. The Json should be [{{"num": ..., "content": ..., "marks": ..., "level": ...}}, ...]
1. **Meta-level titles or rubric headers**, typically unrelated to any specific question:
   - Appear at the start of the mark scheme or before the first question.
   - Contain phrases like:
     * "Components using point-based marking"
     * "Presentation of mark scheme"
     * "General marking principles"
     * "Marking instructions"
     * "Annotation rules"
     * "Own figure rule"
   - These are general examiner guidelines, not question-specific content.

2. **Generalised examiner-focused language**, which talks about how examiners should behave, not what candidates must do:
   - Sentences beginning with or containing:
     * "We give credit where..."
     * "We do not give credit where..."
     * "Credit answers which..."
     * "DO/DO NOT credit..."
     * "Candidates must..."
     * "Marks are awarded for..."
     * "For point marking, ticks can be used..."
     * "The mark scheme will show..."
   - These phrases describe marking behaviour or annotation policy — NOT assessable content.

3. **Structural position**:
   - Usually appear before the first genuine question (e.g., before something like "1(a)(i)" or "Question 1").
   - May use numbering like "1", "2", "3" that represents *section numbers* or *page markers*, NOT question numbers.
   - You MUST distinguish these from real question identifiers:
     * A true question number always corresponds to the actual exam paper numbering.
     * Rubric numbering (like “1. Components using point-based marking”) does **not** link to a question in the paper.

4. Banded marking tables or level descriptors(AO descriptor):
   - Exclude any table or block that defines *mark bands* or *levels of response* used for general grading, rather than marking a specific question.
   - These sections can appear under many headings, such as:
     * "Levels of response", "Generic mark bands", "Band descriptors", "Marking bands", or simply "Table A", "Table B"
     * Or any header that contains words like "Level", "Band", "Descriptor", "Mark range", or "Performance criteria"
   - Structural cues:
     * The block is organized as a table or list with three recurring columns or elements:
         1. A **level indicator** (e.g. “Level 3”, “Band 2”, “Mark 1–2”, “0”)
         2. A **qualitative description** (e.g. “A detailed knowledge and understanding...”, “Limited analysis...”, “No creditable response.”)
         3. A **mark range** (e.g. “6–8”, “3–5”, “1–2”, “0”)
     * Often appears before or after questions that refer to it (“use Table A/B” or “use the levels of response table”).
   - Semantic cues:
     * The text describes *how examiners award marks* based on overall response quality,
       not *what candidates must answer*.
     * Language focuses on **evaluation quality**, **clarity**, **organisation**, **knowledge and understanding**, etc.
       (e.g. “well-organised”, “developed and detailed analysis”, “makes reasoned judgement”).
   - Action:
     * Skip the entire block, including all its level rows.
     * Do NOT treat the level numbers or marks as question numbers.
     * Do NOT attach any true questions beneath these.
     * Resume parsing once you encounter genuine question text (e.g. starts with a command verb or question numbering like “1(a)”).

5. **Action rule**:
   - Completely skip these rubric sections and all their sub-points.
   - Do NOT create JSON entries for them.
   - Do NOT assign any `"num"`, `"marks"`, or `"level"`.
   - Resume parsing only when you detect a question reference consistent with the question paper numbering.

6. **Important safeguard:**
   - Do NOT skip a question merely because it contains references to general marking criteria 
     (e.g., “apply the general principles of point marking” or “refer to the own figure rule”).
   - In such cases, the surrounding text is still question-specific and must be included.
   - Only skip when the *entire block itself* is clearly a marking guideline section, not a question.

Rules:
1. "num":
   - Extract ONLY the local question number token exactly as it appears at the start 
     of the mark scheme entry (1, (a), (ii), Q3, etc.).
   - Do not merge parent and child numbers.
   - Parent nodes (like 1, (a)) should exist structurally but not duplicate the full text;
     the actual award criteria belong to the lowest numbered level (e.g., (i)).
   - If no explicit number is shown but the text clearly belongs to a sub-question, 
     assign an inferred numbering token logically to preserve hierarchy.
   - Continuations without number: "num": null.

2. "content":
   - Copy the full mark scheme text that describes how marks are awarded.
   - Include descriptive criteria, indicative points, examples of acceptable answers, 
     and any “max/min” type constraints.
   - Exclude standalone rubric text that applies to the whole paper 
     (e.g., generic Assessment Objectives, “marks are awarded for quality of written communication,” etc.).

3. "marks":
   - Extract the mark value if explicitly shown in square brackets [x] or in formats like "(4 marks)".
   - If a block has multiple allocations (e.g., [2] + [2]), sum them up.
   - If the mark scheme presents an "Either ... Or ..." type of option (e.g. "EITHER explain X [5] OR explain Y [5]"):
        - Treat the whole block as **one parent question**.
        - Set the "marks" to the full allocation by methods such as summation(e.g. 5). 
        - Their "content" should contain the text of the option (criteria for X or Y).
        - This way, marks are not duplicated across both options: only one record of it exists.
   - If no marks explicitly shown, set "marks": null.

4. "level":
   - Use hierarchical depth, here is only example mapping(there could be exception so the level should inferred from the given tree structure mainly) 
     - Top-level questions = 1,
     - Subparts ((a)) = 2,
     - Sub-subparts ((i)) = 3,
     - etc.
   - Continuations (num=null) inherit the last node's level.

5. Ignore:
   - Generic front matter (Assessment Objectives, rubric, admin notes).
   - Headers, footers, page numbers.

6. Output ONLY a JSON array.

"""
        return super().build(page_input, input_type, tree_json, last_node_json)


class RevisedQPPromptBuilder(PromptBuilder):
    def __init__(self):
        super().__init__()

    def build(self, page_input, input_type, tree_json, last_node_json):
        self.base_context = f"""
You are a structured question parser for exam papers.

Current context for reasoning:
--------------------------------
Previous tree (hierarchical context across pages):
{tree_json}

Last parsed node (for potential continuation):
{last_node_json}
--------------------------------
Task:
Identify **all question-like blocks** in the current page and output them as an ARRAY of JSON objects.
Each object must follow this schema exactly:
{{
  "num": string | null,
  "content": string,
  "marks": integer | null,
  "level": integer
}}

Output Rules (very strict):
--------------------------------
- Return ONLY a JSON array: [{{...}}, {{...}}, ...]
- No explanations, comments, or extra text.
- Each object must contain **exactly** these four keys and valid types.
- The output must be valid JSON that can be parsed without modification.

--------------------------------
Field Extraction Logic:

1. "num" (Question Number)
   - Extract ONLY the local number or token that appears at the start of a question block.
     e.g. "1", "(a)", "(ii)", "Q3", etc.
   - Do NOT combine parent numbers (e.g. "1(a)" → "(a)").
   - If no explicit number is visible (e.g. continuation lines, OCR truncation, or page overflow), set "num": null.
   - For nested or overlapping numbers like "1(a)(i)":
      - **Always** create one JSON object for each unique number token in the chain:
          - "1" → top-level question
          - "(a)" → its sub-question
          - "(i)" → its sub-sub-question (the actual content holder)
      - Each parent placeholder ("1", "(a)") **must** appear once in the output array
        **before** its corresponding child node.
      - Parent placeholders have `"content": ""` and `"marks": null`.
      - Their `"level"` should follow nesting depth:
          - "1" → level 1  
          - "(a)" → level 2  
          - "(i)" → level 3
      - The innermost node (lowest-level number, e.g. "(i)") carries the actual question text and marks.
      - Never duplicate the same parent placeholder more than once within a single page parse.
   - For unusual formats ("Part II(i)", "Ex.1"), extract the recognizable inner unit ("(i)") or fallback to null.

2. "content"
   - Include the full question text, including background, context, and subparts.
   - Exclude explicit mark labels (e.g. "[4]", "(4 marks)").
   - If "Either ... Or ..." appears within the same question number block:
       - Treat it as one question, include both parts in "content".
       - Do NOT double-count marks if both parts have the same mark value.
   - If "Either" and "Or" each appear under different question numbers:
       - Treat them as separate questions, each with its own JSON object.

3. "marks"
   - Extract integer marks where clearly indicated: "[4]", "(4 marks)" → 4.
   - Normalize multiple allocations:
       - If multiple mark notations appear for **the same question**, sum them (e.g. "[2] ... [3]" → 5).
       - For "Either/Or" in the same question num, take one representative mark (not the sum).
   - If marks are missing or ambiguous, set to null.

4. "level"
   - Infer hierarchical level based on both numbering pattern **and** the context in `tree_json`.
   - Use `tree_json` to determine global level alignment:
       - If the previous page ended with "1(b)" (level=2) and current page starts with "(i)", infer level=3.
       - If numbering style changes, align it consistently with prior hierarchy.
   - Continuations (num=null) inherit the last known level.
   - If level cannot be inferred confidently, fallback to the last known node's level.

--------------------------------
Continuation Rules:
- If the first few lines of the current page have no question number but clearly continue a previous question,
  mark them as continuation blocks:
  {{
    "num": null,
    "content": "...",
    "marks": null,
    "level": (same as last_node_json.level)
  }}
- This continuation rule can also apply within a page when a paragraph clearly extends a previous question
  without a new number.

--------------------------------
Filtering Rules:
- Ignore non-question text: headers, footers, page numbers, admin text, "Turn over", etc.
- Do not output empty arrays unless truly no question content exists.

--------------------------------
Output format (MUST follow exactly):
[
  {{"num": "1", "content": "Question text...", "marks": 2, "level": 1}},
  {{"num": "(a)", "content": "Sub-question text...", "marks": 3, "level": 2}},
  {{"num": "(b)", "content": "Another question...", "marks": 4, "level": 2}},
  {{"num": "(i)", "content": "Nested sub-question", "marks": 1, "level": 3}}
]

Your output must be a valid JSON array as above — no commentary, no text before or after.
"""
        return super().build(page_input, input_type, tree_json, last_node_json)