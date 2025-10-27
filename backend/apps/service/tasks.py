# Task.py
import torch

# T = TypeVar("T")
#
#
# class Response(TypedDict, Generic[T], total=False):
#     success: bool
#     data: Optional[T]
#     error: Optional[str]
#
#
# class SingletonModelFactory:
#     """
#     Lazily loads and caches Hugging Face models and tokenizers per worker process.
#     """
#     _instances: Dict[Tuple[str, Optional[str]], Tuple[PreTrainedTokenizer, PreTrainedModel, str]] = {}
#
#     @classmethod
#     def get(
#             cls, model_name: str, device: Optional[str] = None
#     ) -> Tuple[PreTrainedTokenizer, PreTrainedModel, str]:
#         key = (model_name, device)
#         if key not in cls._instances:
#             tokenizer = AutoTokenizer.from_pretrained(model_name)
#             model = AutoModel.from_pretrained(model_name)
#
#             if device is None:
#                 device = "cuda" if torch.cuda.is_available() else "cpu"
#             model.to(device)
#             model.eval()
#
#             cls._instances[key] = (tokenizer, model, device)
#         return cls._instances[key]
#
#
# @dramatiq.actor(queue_name="huggingface-embedding", max_retries=0, time_limit=10000)
# def generate_embedding(
#         text: str,
#         model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
# ) -> Response:
#     """
#     Generate an embedding for the given text using the specified Hugging Face model.
#     Returns a standardized dictionary with 'success', 'data', and 'error' fields.
#     """
#     try:
#         tokenizer, model, device = SingletonModelFactory.get(model_name)
#
#         inputs = tokenizer(text, return_tensors="pt", truncation=True, padding=True)
#         inputs = {k: v.to(device) for k, v in inputs.items()}
#
#         with torch.no_grad():
#             outputs = model(**inputs)
#             embedding_tensor = outputs.last_hidden_state[:, 0, :]
#             embedding = embedding_tensor.cpu().tolist()
#
#         return {
#             "success": True,
#             "data": embedding
#         }
#
#     except Exception as e:
#         return {
#             "success": False,
#             "error": str(e)
#         }

from backend.apps.service.orchestrators.awaitable import awaitable_actor
from transformers import AutoTokenizer, AutoModel

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_tokenizer = None
_model = None


def get_model_and_tokenizer():
    global _tokenizer, _model
    if _tokenizer is None or _model is None:
        print("Loading HF model and tokenizer...")
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        _model = AutoModel.from_pretrained(MODEL_NAME)
        _model.eval()  # Set to eval mode for inference
    return _tokenizer, _model


@awaitable_actor(queue_name="embeddings")
def generate_embedding(text: str) -> list[float]:
    tokenizer, model = get_model_and_tokenizer()
    inputs = tokenizer(text, return_tensors="pt", truncation=True, padding=True)

    with torch.no_grad():
        outputs = model(**inputs)
        embeddings = outputs.last_hidden_state.mean(dim=1).squeeze().tolist()

    print(f"[Task_embedding] Generated for text: {text[:50]}...")
    return embeddings

# @actor(queue_name="embeddings", store_results=True)
# def generate_embedding(text: str) -> list[float]:
#     time.sleep(1)
#     print(f"[Task_embedding] Generated for {text}! ")
#     return [0.1, 0.2, 0.3]
