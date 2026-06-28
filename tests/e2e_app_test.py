import json
import os
import io
import time
import threading
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP_ROOT = ROOT / ".e2e_tmp"
TMP_ROOT.mkdir(exist_ok=True)
os.environ["TMP"] = str(TMP_ROOT)
os.environ["TEMP"] = str(TMP_ROOT)

from pypdf import PdfReader
from streamlit.testing.v1 import AppTest

import auth


def make_pdf_bytes(lines):
    escaped_lines = [
        line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        for line in lines
    ]
    stream = "BT /F1 12 Tf 72 720 Td " + " Tj T* ".join(
        f"({line})" for line in escaped_lines
    ) + " Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            "<< /Type /Page /Parent 2 0 R "
            "/Resources << /Font << /F1 4 0 R >> >> "
            "/MediaBox [0 0 612 792] /Contents 5 0 R >>"
        ),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream.encode('utf-8'))} >>\nstream\n{stream}\nendstream",
    ]

    pdf = "%PDF-1.4\n"
    offsets = []

    for index, item in enumerate(objects, 1):
        offsets.append(len(pdf.encode("utf-8")))
        pdf += f"{index} 0 obj\n{item}\nendobj\n"

    xref_offset = len(pdf.encode("utf-8"))
    pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"

    for offset in offsets:
        pdf += f"{offset:010d} 00000 n \n"

    pdf += (
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    )

    return pdf.encode("utf-8")


def get_text_input(app, label):
    for text_input in app.text_input:
        if text_input.label == label:
            return text_input

    raise AssertionError(f"Text input not found: {label}")


def assert_no_streamlit_exceptions(app):
    exceptions = [exception.value for exception in app.exception]
    assert not exceptions, exceptions


def login(app):
    get_text_input(app, "Username").set_value("admin")
    get_text_input(app, "Password").set_value("admin")

    for button in app.button:
        if button.label == "Sign in":
            button.click().run(timeout=30)
            return app

    raise AssertionError("Sign in button not found")


class MockAnthropicHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        self.__class__.requests.append(json.loads(body))

        response = {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "MiniMax-M2.7",
            "content": [
                {
                    "type": "text",
                    "text": "The favorite project is Solar Forecast Dashboard."
                }
            ],
            "stop_reason": "end_turn",
        }
        response_bytes = json.dumps(response).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def log_message(self, format, *args):
        return


def start_mock_anthropic_server():
    MockAnthropicHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), MockAnthropicHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_pdf_upload_duplicate_reuse_and_minimax_answer(tmp_path):
    store_path = Path(
        os.environ.get("DOC_STORE_PATH", tmp_path / "ingested_documents.json")
    )
    os.environ["DOC_STORE_PATH"] = str(store_path)
    os.environ["USERS_PATH"] = str(tmp_path / f"users_{int(time.time())}.json")
    auth.USERS_PATH = os.environ["USERS_PATH"]

    created, message = auth.register_user(
        "new_user",
        "secret123",
        "secret123",
    )
    assert created, message
    assert auth.authenticate("new_user", "secret123")
    assert not auth.authenticate("new_user", "wrong-password")

    server = start_mock_anthropic_server()
    os.environ["ANTHROPIC_BASE_URL"] = (
        f"http://127.0.0.1:{server.server_port}/anthropic"
    )
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "test-token"
    os.environ["ANTHROPIC_MODEL"] = "MiniMax-M2.7"

    pdf_bytes = make_pdf_bytes(
        [
            "Candidate name is Asha Rao.",
            "Favorite project is Solar Forecast Dashboard.",
            "The project predicts solar generation from weather data.",
            "Email is asha@example.com.",
        ]
    )
    second_pdf_bytes = make_pdf_bytes(
        [
            "Project name is Robotics Inventory Tracker.",
            "The tool manages spare parts for automation labs.",
        ]
    )
    extracted_text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    assert "Solar Forecast Dashboard" in extracted_text

    try:
        app = AppTest.from_file(str(ROOT / "app.py"))
        app.run(timeout=30)
        assert_no_streamlit_exceptions(app)
        login(app)
        assert_no_streamlit_exceptions(app)

        app.file_uploader[0].upload(
            "asha_resume.pdf",
            pdf_bytes,
            "application/pdf"
        ).run(timeout=30)
        assert_no_streamlit_exceptions(app)

        store = json.loads(store_path.read_text(encoding="utf-8"))
        assert len(store) == 1
        first_store_snapshot = store.copy()

        get_text_input(app, "Ask your question...").set_value(
            "What is the favorite project?"
        ).run(timeout=90)
        assert_no_streamlit_exceptions(app)

        page_text = "\n".join(
            item.value
            for collection in [app.markdown, app.text, app.info, app.success]
            for item in collection
            if getattr(item, "value", None)
        )
        assert "The favorite project is Solar Forecast Dashboard." in page_text
        assert "Model error:" not in page_text
        assert MockAnthropicHandler.requests
        assert "Solar Forecast Dashboard" in json.dumps(
            MockAnthropicHandler.requests[-1]
        )

        get_text_input(app, "Ask your question...").set_value("").run(timeout=30)
        app.file_uploader[0].clear().run(timeout=30)
        app.file_uploader[0].upload(
            "asha_resume.pdf",
            pdf_bytes,
            "application/pdf"
        ).run(timeout=30)
        assert_no_streamlit_exceptions(app)

        store_after_duplicate_upload = json.loads(
            store_path.read_text(encoding="utf-8")
        )
        assert store_after_duplicate_upload == first_store_snapshot

        duplicate_page_text = "\n".join(
            item.value
            for item in app.info
            if getattr(item, "value", None)
        )
        assert "Already in knowledge base" in duplicate_page_text

        get_text_input(app, "Ask your question...").set_value("").run(timeout=30)
        app.file_uploader[0].clear().run(timeout=30)
        app.file_uploader[0].upload(
            "robotics_notes.pdf",
            second_pdf_bytes,
            "application/pdf"
        ).run(timeout=30)
        assert_no_streamlit_exceptions(app)

        store_after_second_upload = json.loads(
            store_path.read_text(encoding="utf-8")
        )
        assert len(store_after_second_upload) == 2

        get_text_input(app, "Ask your question...").set_value(
            "What manages spare parts?"
        ).run(timeout=90)
        assert_no_streamlit_exceptions(app)
        assert "Robotics Inventory Tracker" in json.dumps(
            MockAnthropicHandler.requests[-1]
        )

        app = AppTest.from_file(str(ROOT / "app.py"))
        app.run(timeout=30)
        assert_no_streamlit_exceptions(app)
        login(app)
        assert_no_streamlit_exceptions(app)

        saved_document_text = "\n".join(
            item.value
            for collection in [app.markdown, app.caption]
            for item in collection
            if getattr(item, "value", None)
        )
        assert "asha_resume.pdf" in saved_document_text
        assert "robotics_notes.pdf" in saved_document_text
        assert any(button.label == "Remove" for button in app.button)

        for button in app.button:
            if button.label == "Remove":
                button.click().run(timeout=30)
                break

        assert_no_streamlit_exceptions(app)
        store_after_remove = json.loads(store_path.read_text(encoding="utf-8"))
        assert len(store_after_remove) == 1
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    temp_dir = ROOT / ".e2e_tmp"
    temp_dir.mkdir(exist_ok=True)
    os.environ["DOC_STORE_PATH"] = str(
        temp_dir / f"ingested_documents_{int(time.time())}.json"
    )

    test_pdf_upload_duplicate_reuse_and_minimax_answer(temp_dir)

    print("E2E test passed", flush=True)
    os._exit(0)
