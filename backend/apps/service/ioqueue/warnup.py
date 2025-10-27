import io
import threading
import pdfplumber

_pdfium_initialized = False
_pdfium_init_lock = threading.Lock()


def _create_minimal_pdf() -> bytes:
    """
    创建一个极简 PDF（1 页空白页），完全不依赖任何第三方库
    PDF 1.1 最小化结构，能被绝大多数 PDF 渲染器正确解析
    """
    content = b"""%PDF-1.1
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R >>
endobj
4 0 obj
<< /Length 44 >>
stream
BT
/F1 12 Tf
72 720 Td
(Hello PDFium Warmup) Tj
ET
endstream
endobj
xref
0 5
0000000000 65535 f 
0000000010 00000 n 
0000000061 00000 n 
0000000116 00000 n 
0000000197 00000 n 
trailer
<< /Size 5 /Root 1 0 R >>
startxref
282
%%EOF
"""
    return content


def _warmup_pdfium():
    """
    在启动时执行一次 PDFium 预热，确保初始化在单线程里完成
    """
    global _pdfium_initialized
    if _pdfium_initialized:
        return

    with _pdfium_init_lock:
        if _pdfium_initialized:
            return

        dummy_pdf = _create_minimal_pdf()
        with pdfplumber.open(io.BytesIO(dummy_pdf)) as pdf:
            page = pdf.pages[0]
            page.to_image(resolution=50)  # 低分辨率渲染，触发初始化

        _pdfium_initialized = True
        print("warmed up!")


def warmup():
    _warmup_pdfium()
