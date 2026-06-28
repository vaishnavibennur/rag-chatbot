import streamlit as st
from pypdf import PdfReader
import faiss
import numpy as np
import re
import difflib
import os
import sys
import html
import json
import hashlib
import time
import csv
import io
import shutil
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import fitz
import openpyxl
import pytesseract
from docx import Document
from PIL import Image, UnidentifiedImageError

# AUTO-DETECT TESSERACT PATH
def setup_tesseract():
    possible_paths = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        shutil.which("tesseract"),
    ]
    
    for path in possible_paths:
        if path and os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            print(f"Tesseract found at: {path}")
            return path
    
    print("Tesseract not found. Checked:")
    for p in possible_paths:
        if p:
            print(f"  - {p}")

setup_tesseract()

sys.path.insert(0, os.path.dirname(__file__))

from auth import logout_button, require_login

DOC_STORE_PATH = os.getenv(
    "DOC_STORE_PATH",
    os.path.join("data", "ingested_documents.json")
)
STYLE_PATH = os.path.join(os.path.dirname(__file__), "styles.css")
EMBEDDING_DIMENSION = 768
SUPPORTED_UPLOAD_TYPES = [
    "pdf",
    "txt",
    "md",
    "csv",
    "json",
    "docx",
    "xlsx",
    "png",
    "jpg",
    "jpeg",
    "webp",
    "bmp",
    "tiff",
]

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "how", "i", "in", "is", "it", "of", "on", "or", "that", "the",
    "this", "to", "was", "what", "when", "where", "which", "who", "why",
    "with", "you", "your", "me", "my", "tell", "about", "give", "show",
}


def load_styles():
    try:
        with open(STYLE_PATH, "r", encoding="utf-8") as style_file:
            st.markdown(
                f"<style>{style_file.read()}</style>",
                unsafe_allow_html=True,
            )
    except OSError:
        st.warning("Custom styles could not be loaded.")


def log_ingest(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [ingest] {message}", flush=True)


def split_text(text, chunk_size=1200, overlap=150):
    text = re.sub(r"\r", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = text.split("\n")
    chunks = []
    current_chunk = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if len(line) > chunk_size:
            words = line.split()
            for word in words:
                if len(current_chunk) + len(word) + 1 <= chunk_size:
                    current_chunk += word + " "
                else:
                    if current_chunk.strip():
                        chunks.append(current_chunk.strip())
                    current_chunk = (current_chunk[-overlap:] + " " + word + " ").strip() + " "
            continue

        if len(current_chunk) + len(line) + 1 <= chunk_size:
            current_chunk += line + "\n"
        else:
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
            prefix = current_chunk[-overlap:].strip()
            current_chunk = (prefix + "\n" + line + "\n") if prefix else (line + "\n")

    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks


def is_match(query, keywords):
    words = re.findall(r"[a-zA-Z]+", query.lower())
    for word in words:
        match = difflib.get_close_matches(word, keywords, n=1, cutoff=0.7)
        if match:
            return match[0]
    return None


def is_greeting(query):
    normalized = re.sub(r"[^a-zA-Z\s]", " ", query.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()

    greeting_phrases = [
        "hi",
        "hello",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
        "how are you",
        "how r you",
        "how are u",
        "how r u",
        "what's up",
        "whats up",
    ]

    return any(
        normalized == phrase
        or normalized.startswith(f"{phrase} ")
        for phrase in greeting_phrases
    )


def detect_requested_language(query):
    normalized = query.lower()

    language_keywords = {
        "Hindi": ["hindi", "हिंदी", "हिन्दी"],
        "Kannada": ["kannada", "ಕನ್ನಡ"],
        "Tamil": ["tamil", "தமிழ்"],
        "Telugu": ["telugu", "తెలుగు"],
        "English": ["english"],
    }

    for language, keywords in language_keywords.items():
        if any(keyword in normalized for keyword in keywords):
            return language

    return None


def get_effective_language(query):
    requested_language = detect_requested_language(query)

    if requested_language:
        return requested_language

    return st.session_state.get("response_language", "Auto")


def get_language_instruction(language):
    if not language or language == "Auto":
        return ""

    return f" Answer in {language}. Keep names, emails, phone numbers, percentages, and technical terms unchanged."


def greeting_response(language):
    greetings = {
        "Hindi": "नमस्ते! मैं ठीक हूं। मैं आपकी कैसे मदद कर सकता हूं?",
        "Kannada": "ನಮಸ್ಕಾರ! ನಾನು ಚೆನ್ನಾಗಿದ್ದೇನೆ. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
        "Tamil": "வணக்கம்! நான் நன்றாக இருக்கிறேன். நான் உங்களுக்கு எப்படி உதவலாம்?",
        "Telugu": "నమస్తే! నేను బాగున్నాను. నేను మీకు ఎలా సహాయం చేయగలను?",
    }

    return greetings.get(language, "Hello! I'm doing well. How can I help you today?")


def submit_query():
    st.session_state.submitted_query = st.session_state.get("query_input", "")
    st.session_state.query_input = ""


def load_document_store():
    if not os.path.exists(DOC_STORE_PATH):
        return {}
    try:
        with open(DOC_STORE_PATH, "r", encoding="utf-8") as store_file:
            return json.load(store_file)
    except (json.JSONDecodeError, OSError):
        return {}


def save_document_store(store):
    os.makedirs(os.path.dirname(DOC_STORE_PATH), exist_ok=True)
    with open(DOC_STORE_PATH, "w", encoding="utf-8") as store_file:
        json.dump(store, store_file, indent=2, ensure_ascii=False)


def get_file_hash(uploaded_file):
    return hashlib.sha256(uploaded_file.getvalue()).hexdigest()


def get_file_extension(file_name):
    return os.path.splitext(file_name or "")[1].lower().lstrip(".")


def configure_tesseract():
    # Force set the path directly
    tesseract_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    
    if os.path.exists(tesseract_path):
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
        log_ingest(f"✓ Tesseract configured at: {tesseract_path}")
        return tesseract_path
    
    # Fallback: Try other common locations
    possible_paths = [
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        shutil.which("tesseract"),
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ]
    
    for path in possible_paths:
        if path and os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            log_ingest(f"✓ Tesseract found at: {path}")
            return path
    
    error_msg = f"Tesseract not found. Primary path checked: {tesseract_path}"
    log_ingest(error_msg)
    raise RuntimeError(error_msg)


def extract_image_text(image_bytes):
    configure_tesseract()
    try:
        image = Image.open(io.BytesIO(image_bytes))
    except UnidentifiedImageError as error:
        raise RuntimeError("This image format could not be read for OCR.") from error

    return pytesseract.image_to_string(image).strip()


def ocr_pdf_pages(pdf_bytes):
    configure_tesseract()
    text_parts = []

    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf_document:
        total_pages = pdf_document.page_count
        for page_index in range(total_pages):
            log_ingest(f"OCR extracting page {page_index + 1}/{total_pages}")
            page = pdf_document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.open(io.BytesIO(pixmap.tobytes("png")))
            page_text = pytesseract.image_to_string(image).strip()
            if page_text:
                text_parts.append(f"[Page {page_index + 1}]\n{page_text}")

    return "\n\n".join(text_parts)


def extract_pdf_text(uploaded_file):
    text = ""
    reader = PdfReader(uploaded_file)
    total_pages = len(reader.pages)
    log_ingest(f"PDF has {total_pages} pages")

    for page_number, page in enumerate(reader.pages, start=1):
        log_ingest(f"Extracting page {page_number}/{total_pages}")
        extracted = page.extract_text()
        if extracted:
            text += f"[Page {page_number}]\n{extracted}\n"

    return text


def extract_text_file(file_bytes):
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="replace")


def extract_csv_text(file_bytes):
    text = extract_text_file(file_bytes)
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if row:
            rows.append(" | ".join(cell.strip() for cell in row if cell.strip()))
    return "\n".join(rows)


def extract_json_text(file_bytes):
    parsed = json.loads(extract_text_file(file_bytes))
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def extract_docx_text(file_bytes):
    document = Document(io.BytesIO(file_bytes))
    paragraphs = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    table_rows = []

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                table_rows.append(" | ".join(cells))

    return "\n".join(paragraphs + table_rows)


def extract_xlsx_text(file_bytes):
    workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
    sheets = []

    for sheet in workbook.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            values = [str(value).strip() for value in row if value is not None and str(value).strip()]
            if values:
                rows.append(" | ".join(values))
        if rows:
            sheets.append(f"Sheet: {sheet.title}\n" + "\n".join(rows))

    workbook.close()
    return "\n\n".join(sheets)


def extract_document_text(uploaded_file):
    file_bytes = uploaded_file.getvalue()
    file_extension = get_file_extension(uploaded_file.name)

    if file_extension == "pdf":
        log_ingest(f"Starting PDF text extraction for '{uploaded_file.name}'")
        text = extract_pdf_text(io.BytesIO(file_bytes))
        if text.strip():
            return text

        log_ingest(f"No embedded text found in '{uploaded_file.name}', trying OCR")
        return ocr_pdf_pages(file_bytes)

    if file_extension in {"png", "jpg", "jpeg", "webp", "bmp", "tiff"}:
        log_ingest(f"Starting image OCR for '{uploaded_file.name}'")
        return extract_image_text(file_bytes)

    if file_extension in {"txt", "md"}:
        return extract_text_file(file_bytes)

    if file_extension == "csv":
        return extract_csv_text(file_bytes)

    if file_extension == "json":
        return extract_json_text(file_bytes)

    if file_extension == "docx":
        return extract_docx_text(file_bytes)

    if file_extension == "xlsx":
        return extract_xlsx_text(file_bytes)

    raise RuntimeError(f"Unsupported file type: .{file_extension or 'unknown'}")


def get_or_ingest_document(uploaded_file, store):
    file_hash = get_file_hash(uploaded_file)
    log_ingest(f"Checking document '{uploaded_file.name}' ({file_hash[:12]})")

    if file_hash in store:
        log_ingest(f"Reusing already ingested document '{uploaded_file.name}'")
        return store[file_hash], True

    file_extension = get_file_extension(uploaded_file.name)
    try:
        text = extract_document_text(uploaded_file)
    except RuntimeError as error:
        text = f"Could not extract text from this file. {error}"
        log_ingest(str(error))

    log_ingest(f"Extracted {len(text)} characters from '{uploaded_file.name}'")

    log_ingest(f"Splitting '{uploaded_file.name}' into chunks")
    chunks = split_text(text)
    log_ingest(f"Created {len(chunks)} chunks for '{uploaded_file.name}'")

    if not chunks:
        text = (
            "No extractable text was found in this file. "
            "For scanned documents or images, OCR needs Tesseract OCR installed."
        )
        chunks = [text]
        log_ingest(f"No extractable text found in '{uploaded_file.name}'")

    document = {
        "file_name": uploaded_file.name,
        "file_type": file_extension,
        "file_hash": file_hash,
        "text": text,
        "chunks": chunks,
        "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    store[file_hash] = document
    save_document_store(store)
    log_ingest(f"Saved '{uploaded_file.name}' to document store")

    return document, False


def format_document_label(document):
    chunk_count = len(document.get("chunks", []))
    ingested_at = document.get("ingested_at", "unknown time")
    return f"{document.get('file_name', 'Untitled PDF')} ({chunk_count} chunks, {ingested_at})"


def build_knowledge_base(store):
    documents = list(store.values())
    kb_chunks = []
    kb_texts = []
    chunk_sources = []

    for document in documents:
        file_name = document.get("file_name", "Untitled PDF")
        text = document.get("text", "")
        chunks = document.get("chunks", [])
        kb_texts.append(text)

        for chunk_number, chunk in enumerate(chunks, start=1):
            kb_chunks.append(f"Source: {file_name}\n\n{chunk}")
            chunk_sources.append({
                "file_name": file_name,
                "chunk_number": chunk_number,
                "file_hash": document.get("file_hash", ""),
            })

    return {
        "documents": documents,
        "chunks": kb_chunks,
        "text": "\n".join(kb_texts),
        "chunk_sources": chunk_sources,
    }


def tokenize_for_embedding(text):
    return re.findall(r"[a-zA-Z0-9]+", text.lower())


def encode_texts(texts):
    vectors = np.zeros((len(texts), EMBEDDING_DIMENSION), dtype="float32")

    for row, text in enumerate(texts):
        tokens = tokenize_for_embedding(text)
        features = tokens + [f"{tokens[index]}_{tokens[index + 1]}" for index in range(len(tokens) - 1)]

        for feature in features:
            digest = hashlib.md5(feature.encode("utf-8")).digest()
            column = int.from_bytes(digest[:4], "little") % EMBEDDING_DIMENSION
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vectors[row, column] += sign

    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0

    return vectors / norms


def load_env_file(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(__file__), ".env")

    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name and (name.startswith("OPENAI_") or name.startswith("TESSERACT_") or name.startswith("ANTHROPIC_") or name not in os.environ):
                os.environ[name] = value


load_env_file()


def get_config_value(*names):
    for name in names:
        try:
            value = st.secrets.get(name)
        except Exception:
            value = None
        if value:
            return value
        value = os.getenv(name)
        if value:
            return value
    return None


def get_api_timeout_seconds():
    timeout_ms = get_config_value("API_TIMEOUT_MS") or "300000"
    try:
        return max(1, int(timeout_ms) / 1000)
    except ValueError:
        return 300


def extract_anthropic_text(response_data):
    content = response_data.get("content", [])
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(part for part in parts if part).strip()


def answer_with_anthropic(query, context, response_language="Auto"):
    model_name = get_config_value("ANTHROPIC_MODEL", "anthropic_model") or "MiniMax-M2.7"
    base_url = (get_config_value("ANTHROPIC_BASE_URL", "anthropic_base_url") or "https://api.anthropic.com").rstrip("/")
    auth_token = get_config_value("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "anthropic_auth_token", "anthropic_api_key")

    if not auth_token:
        raise RuntimeError("Anthropic auth token is missing.")

    payload = {
        "model": model_name,
        "max_tokens": 700,
        "temperature": 0.1,
        "system": (
            "You are a smart document question-answering assistant. "
            "Use only the provided document context. "
            "Give a direct, helpful answer in simple language. "
            "If the answer is not present in the context, say that it is not available in the document."
            + get_language_instruction(response_language)
        ),
        "messages": [{
            "role": "user",
            "content": f"Question:\n{query}\n\nDocument context:\n{context}"
        }]
    }

    request = Request(
        f"{base_url}/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": auth_token,
            "Authorization": f"Bearer {auth_token}",
            "anthropic-version": "2023-06-01",
        },
        method="POST"
    )

    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=get_api_timeout_seconds()) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(str(error.reason)) from error

    answer = extract_anthropic_text(response_data)
    if not answer:
        raise RuntimeError("The model returned an empty answer.")
    return answer


def get_keywords(query):
    words = re.findall(r"[a-zA-Z0-9]+", query.lower())
    return [word for word in words if word not in STOPWORDS and len(word) > 2]


def answer_from_context(query, retrieved_chunks):
    context = "\n".join(retrieved_chunks)
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+|\n+", context) if sentence.strip()]
    keywords = get_keywords(query)

    if not sentences:
        return "I found related text, but I could not extract a clean answer from it."
    if not keywords:
        return retrieved_chunks[0]

    scored = []
    for position, sentence in enumerate(sentences):
        sentence_lower = sentence.lower()
        score = sum(1 for keyword in keywords if keyword in sentence_lower)
        if score:
            scored.append((score, -position, sentence))

    if not scored:
        return "I could not find a direct answer in the document. Closest matching text:\n\n" + retrieved_chunks[0]

    best_sentences = [sentence for _, _, sentence in sorted(scored, reverse=True)[:4]]
    return " ".join(best_sentences)


def init_feature_state():
    defaults = {
        "search_history": [],
        "saved_answers": [],
        "bookmarked_chunks": [],
        "last_sources": [],
        "last_query_time": None,
        "last_answer_length": 0,
        "streaming_responses": False,
        "response_language": "Auto",
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def estimate_word_count(text_value):
    return len(re.findall(r"\b\w+\b", text_value or ""))


def get_document_statistics(documents, chunks, text_value):
    return {
        "documents": len(documents),
        "chunks": len(chunks),
        "words": estimate_word_count(text_value),
        "characters": len(text_value or ""),
    }


def get_processing_statistics(documents, embeddings):
    scanned_docs = sum(
        1
        for document in documents
        if document.get("chunks", [""])[0].startswith("No extractable text")
    )

    return {
        "processed": len(documents),
        "scanned": scanned_docs,
        "embedding_dimension": embeddings.shape[1] if embeddings is not None else 0,
        "vectors": embeddings.shape[0] if embeddings is not None else 0,
    }


def get_ai_statistics():
    return {
        "questions": len(st.session_state.search_history),
        "saved_answers": len(st.session_state.saved_answers),
        "bookmarks": len(st.session_state.bookmarked_chunks),
        "last_answer_length": st.session_state.last_answer_length,
    }


def get_file_statistics(documents):
    return [
        {
            "File": document.get("file_name", "Untitled PDF"),
            "Chunks": len(document.get("chunks", [])),
            "Words": estimate_word_count(document.get("text", "")),
            "Ingested": document.get("ingested_at", "unknown time"),
        }
        for document in documents
    ]


def get_document_chart_data(documents):
    return [
        {
            "File": document.get("file_name", "Untitled PDF"),
            "Chunks": len(document.get("chunks", [])),
            "Words": estimate_word_count(document.get("text", "")),
        }
        for document in documents
    ]


def normalize_graph_axis(values):
    if not values:
        return []

    minimum = min(values)
    maximum = max(values)

    if minimum == maximum:
        return [50.0 for _ in values]

    return [
        8.0 + ((value - minimum) / (maximum - minimum)) * 84.0
        for value in values
    ]


def get_vector_similarity_rows(embeddings, max_rows=16):
    if embeddings is None or embeddings.shape[0] < 2:
        return []

    selected_vectors = embeddings[:max_rows]
    similarities = np.matmul(selected_vectors, selected_vectors.T)
    rows = []

    for row_index in range(selected_vectors.shape[0]):
        nearest_scores = similarities[row_index].copy()
        nearest_scores[row_index] = -1
        nearest_index = int(np.argmax(nearest_scores))
        rows.append(
            {
                "Chunk": f"Chunk {row_index + 1}",
                "Similarity": round(float(nearest_scores[nearest_index]), 4),
            }
        )

    return rows


def get_vector_graph_html(embeddings, max_nodes=14):
    if embeddings is None or embeddings.shape[0] == 0:
        return ""

    selected_vectors = embeddings[:max_nodes]
    x_values = [float(vector[0]) for vector in selected_vectors]
    y_values = [
        float(vector[1]) if selected_vectors.shape[1] > 1 else 0.0
        for vector in selected_vectors
    ]
    x_positions = normalize_graph_axis(x_values)
    y_positions = normalize_graph_axis(y_values)

    links = []
    if selected_vectors.shape[0] > 1:
        similarities = np.matmul(selected_vectors, selected_vectors.T)
        seen_links = set()

        for row_index in range(selected_vectors.shape[0]):
            nearest_scores = similarities[row_index].copy()
            nearest_scores[row_index] = -1
            nearest_index = int(np.argmax(nearest_scores))
            link_key = tuple(sorted((row_index, nearest_index)))

            if link_key not in seen_links:
                seen_links.add(link_key)
                links.append(
                    {
                        "source": row_index,
                        "target": nearest_index,
                        "score": float(nearest_scores[nearest_index]),
                    }
                )

    line_html = "\n".join(
        (
            f"<line x1='{x_positions[link['source']]:.2f}%' "
            f"y1='{y_positions[link['source']]:.2f}%' "
            f"x2='{x_positions[link['target']]:.2f}%' "
            f"y2='{y_positions[link['target']]:.2f}%'>"
            f"<title>Chunk {link['source'] + 1} to Chunk {link['target'] + 1}: "
            f"{link['score']:.2f}</title></line>"
        )
        for link in links
    )

    node_html = "\n".join(
        (
            f"<div class='vector-node' style='left: {x_positions[index_value]:.2f}%; "
            f"top: {y_positions[index_value]:.2f}%;' "
            f"title='Chunk {index_value + 1}'>{index_value + 1}</div>"
        )
        for index_value in range(selected_vectors.shape[0])
    )

    return f"""
    <div class="vector-graph-card">
        <div class="vector-graph-title">Chunk Similarity Graph</div>
        <div class="vector-graph-subtitle">Each node is a chunk. Lines connect closest semantic neighbors.</div>
        <div class="vector-graph">
            <svg viewBox="0 0 100 100" preserveAspectRatio="none">{line_html}</svg>
            {node_html}
        </div>
    </div>
    """


def get_suggested_questions(documents):
    suggestions = [
        "Summarize the key points across all documents.",
        "What are the most important details in this knowledge base?",
        "List names, dates, percentages, and contact details if available.",
        "Compare the documents and mention any repeated themes.",
    ]

    for document in documents[:3]:
        file_name = document.get("file_name", "this document")
        suggestions.append(f"What are the key details in {file_name}?")

    return suggestions[:8]


def export_chat_text():
    lines = []

    for role, message in st.session_state.messages:
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {message}")

    return "\n\n".join(lines)


def render_stat_grid(title, cards):
    card_html = "".join(
        (
            "<div class='stat-card'>"
            f"<div class='stat-label'>{html.escape(label)}</div>"
            f"<div class='stat-value'>{html.escape(str(value))}</div>"
            "</div>"
        )
        for label, value in cards
    )

    st.markdown(
        f"""
        <div class="section-panel">
            <div class="section-title">{html.escape(title)}</div>
            <div class="stat-grid">{card_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# Page setup
st.set_page_config(page_title="AI Knowledge Assistant", page_icon="🤖", layout="wide")
require_login()
init_feature_state()
load_styles()

# Header
st.markdown("""
<div class="app-hero">
    <h1>AI Knowledge Assistant</h1>
    <p>Upload PDFs, maintain a compact knowledge base, and ask focused questions across all documents.</p>
</div>
""", unsafe_allow_html=True)

# Sidebar
document_store = load_document_store()

with st.sidebar:
    st.header("Knowledge Base")
    logout_button()
    st.session_state.streaming_responses = st.toggle(
        "Streaming responses",
        value=st.session_state.streaming_responses,
        help="Display mode preference for new answers.",
    )
    language_options = ["Auto", "English", "Kannada", "Hindi", "Tamil", "Telugu"]
    if st.session_state.response_language not in language_options:
        st.session_state.response_language = "Auto"

    st.session_state.response_language = st.selectbox(
        "Response language",
        language_options,
        index=language_options.index(st.session_state.response_language),
        help="Keeps the retrieval logic unchanged; use this as a response preference.",
    )
    st.markdown('<div class="info-box">Add PDFs once. Questions search across the full knowledge base.</div>', unsafe_allow_html=True)

    uploaded_files = st.file_uploader("Add PDF documents", type="pdf", accept_multiple_files=True)

    if uploaded_files:
        for uploaded_file in uploaded_files:
            document, loaded_from_store = get_or_ingest_document(uploaded_file, document_store)
            if loaded_from_store:
                st.info(f"Already in knowledge base: {document.get('file_name', uploaded_file.name)}")
            else:
                st.success(f"Added: {document.get('file_name', uploaded_file.name)}")
        document_store = load_document_store()

    stored_documents = list(reversed(list(document_store.values())))

    if stored_documents:
        st.subheader("Documents in KB")
        for document in stored_documents:
            label = format_document_label(document)
            file_hash = document.get("file_hash", label)
            st.markdown(
                (
                    "<div class='doc-card'>"
                    f"<div class='doc-title'>{html.escape(document.get('file_name', 'Untitled PDF'))}</div>"
                    f"<div class='doc-meta'>{len(document.get('chunks', []))} chunks - "
                    f"Ingested {html.escape(document.get('ingested_at', 'unknown time'))}</div>"
                    "</div>"
                ),
                unsafe_allow_html=True
            )
            if st.button("Remove", key=f"remove_{file_hash}"):
                document_store.pop(file_hash, None)
                save_document_store(document_store)
                st.rerun()
    else:
        st.info("Add PDFs to build your knowledge base.")

anthropic_auth_token = get_config_value("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "anthropic_auth_token", "anthropic_api_key")

# Build knowledge base
knowledge_base = build_knowledge_base(document_store)
documents = list(reversed(knowledge_base["documents"]))
chunks = knowledge_base["chunks"]
chunk_sources = knowledge_base["chunk_sources"]
text = knowledge_base["text"]
embeddings = None
index = None

if chunks:
    embeddings = encode_texts(chunks)
    dimension = embeddings.shape[1]
    index = faiss.IndexFlatIP(dimension)
    index.add(embeddings)

    # Chat and Preview columns
    chat_col, preview_col = st.columns([2, 1])

    with chat_col:
        st.subheader("Chat Assistant")

        if "messages" not in st.session_state:
            st.session_state.messages = []

        st.text_input(
            "Ask your question...",
            key="query_input",
            on_change=submit_query
        )
        query = st.session_state.pop("submitted_query", "")

        if query and query != st.session_state.get("last_query"):
            st.session_state.last_query = query
            st.session_state.last_sources = []
            effective_language = get_effective_language(query)

            if is_greeting(query):
                result = greeting_response(effective_language)

            elif chunks:
                keyword = is_match(query.lower(), ["phone", "contact", "number", "email", "address", "career", "linkedin"])

                if keyword in ["phone", "contact", "number"]:
                    phones = re.findall(r'\+?\d[\d\s\-]{8,15}\d', text)
                    result = phones[0] if phones else "Phone number not found"
                elif keyword == "email":
                    emails = re.findall(r'[\w\.-]+@[\w\.-]+\.\w+', text)
                    result = emails[0] if emails else "Email not found"
                elif keyword == "address":
                    lines = [l for l in text.split("\n") if "address" in l.lower() or "karnataka" in l.lower()]
                    result = " ".join(lines) if lines else "Address not found"
                elif keyword == "linkedin":
                    linkedin = re.findall(r'https?://[^\s)]+', text)
                    result = linkedin[0] if linkedin else "LinkedIn not found"
                elif keyword == "career":
                    start, end = text.lower().find("career objective"), text.lower().find("educational qualifications")
                    result = text[start:end] if start != -1 else "Career objective not found"
                else:
                    query_embedding = encode_texts([query])
                    D, I = index.search(query_embedding, k=min(6, len(chunks)))
                    retrieved_indices = [idx for idx in I[0] if idx != -1]
                    retrieved_chunks = [chunks[idx] for idx in retrieved_indices]
                    st.session_state.last_sources = [
                        {
                            "rank": rank,
                            "chunk": chunks[idx],
                            "source": chunk_sources[idx] if idx < len(chunk_sources) else {},
                        }
                        for rank, idx in enumerate(retrieved_indices, start=1)
                    ]

                    if anthropic_auth_token:
                        try:
                            result = answer_with_anthropic(
                                query,
                                "\n\n---\n\n".join(retrieved_chunks),
                                effective_language,
                            )
                        except RuntimeError as error:
                            result = f"MiniMax error: {error}\n\n" + answer_from_context(query, retrieved_chunks)
                    else:
                        result = answer_from_context(query, retrieved_chunks)
            else:
                result = "Please add documents to the knowledge base first."

            st.session_state.messages.append(("user", query))
            st.session_state.messages.append(("bot", result))
            st.session_state.search_history.append(
                {
                    "query": query,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "sources": st.session_state.last_sources,
                }
            )
            st.session_state.last_query_time = time.strftime("%Y-%m-%d %H:%M:%S")
            st.session_state.last_answer_length = len(result)
            st.rerun()

        for message_index, (role, msg) in enumerate(reversed(st.session_state.messages)):
            if role == "user":
                st.markdown(f"<div class='user-msg'>{html.escape(msg)}</div>", unsafe_allow_html=True)
            else:
                st.markdown(f"<div class='bot-msg'>{msg}</div>", unsafe_allow_html=True)
                if st.button("Save important answer", key=f"save_answer_{message_index}"):
                    st.session_state.saved_answers.append(
                        {
                            "answer": msg,
                            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                    st.success("Answer saved")

    with preview_col:
        st.subheader("Preview")
        if chunks:
            st.success(f"{len(documents)} documents loaded")
            for document in documents:
                with st.expander(document.get("file_name", "Untitled PDF")):
                    st.caption(f"{len(document.get('chunks', []))} chunks - {document.get('ingested_at', 'unknown time')}")
                    st.write(document.get("text", "")[:1000])
            with st.expander("View Combined Text"):
                st.write(text)
        else:
            st.info("Add PDFs to preview your knowledge base")

    st.markdown("---")

    with st.expander("Analytics dashboard", expanded=False):
        document_stats = get_document_statistics(documents, chunks, text)
        processing_stats = get_processing_statistics(documents, embeddings)
        ai_stats = get_ai_statistics()
        file_stats = get_file_statistics(documents)

        st.subheader("Dashboard")
        render_stat_grid(
            "Overview",
            [
                ("Documents", len(documents)),
                ("Chunks", len(chunks)),
                ("Vectors", embeddings.shape[0]),
                ("Questions", ai_stats["questions"]),
            ],
        )

        dashboard_tabs = st.tabs(
            [
                "Statistics",
                "AI Tools",
                "Saved",
                "Sources",
                "Privacy",
                "Visualization",
            ]
        )

        with dashboard_tabs[0]:
            render_stat_grid(
                "Document Statistics",
                [
                    ("Documents", document_stats["documents"]),
                    ("Chunks", document_stats["chunks"]),
                    ("Words", document_stats["words"]),
                    ("Characters", document_stats["characters"]),
                ],
            )
            render_stat_grid(
                "Processing Statistics",
                [
                    ("Processed Files", processing_stats["processed"]),
                    ("OCR Needed", processing_stats["scanned"]),
                    ("Embedding Dim", processing_stats["embedding_dimension"]),
                    ("Vectors", processing_stats["vectors"]),
                ],
            )
            render_stat_grid(
                "AI Statistics",
                [
                    ("Questions", ai_stats["questions"]),
                    ("Saved Answers", ai_stats["saved_answers"]),
                    ("Bookmarked Chunks", ai_stats["bookmarks"]),
                    ("Last Answer Chars", ai_stats["last_answer_length"]),
                ],
            )

            with st.expander("File Statistics", expanded=False):
                st.dataframe(file_stats, width="stretch")

        with dashboard_tabs[1]:
            st.subheader("AI Suggested Questions")
            for suggested_question in get_suggested_questions(documents):
                st.markdown(f"- {suggested_question}")

            st.subheader("Multilingual Support")
            st.info(
                f"Current response preference: {st.session_state.response_language}. "
                "The selected language is passed to the AI answer prompt. Retrieval still uses the same knowledge base."
            )

        with dashboard_tabs[2]:
            st.subheader("Save Important Answers")
            if st.session_state.saved_answers:
                for saved_index, saved_answer in enumerate(st.session_state.saved_answers, start=1):
                    with st.expander(f"Saved answer {saved_index} - {saved_answer['time']}"):
                        st.write(saved_answer["answer"])
            else:
                st.info("Use 'Save important answer' below any assistant response.")

            st.subheader("Search History")
            if st.session_state.search_history:
                for history_item in reversed(st.session_state.search_history[-20:]):
                    st.markdown(f"- **{history_item['time']}** - {html.escape(history_item['query'])}")
            else:
                st.info("Search history will appear after you ask questions.")

            st.subheader("Session Storage")
            st.markdown(
                (
                    f"You have **{len(st.session_state.messages)} chat messages**, "
                    f"**{len(st.session_state.search_history)} search history item(s)**, "
                    f"**{len(st.session_state.saved_answers)} saved answer(s)**, and "
                    f"**{len(st.session_state.bookmarked_chunks)} bookmarked chunk(s)** in this session."
                )
            )

            st.download_button(
                "Export Chat",
                data=export_chat_text(),
                file_name="chat_export.txt",
                mime="text/plain",
                width="stretch",
            )

        with dashboard_tabs[3]:
            st.subheader("Source Highlighting")
            if st.session_state.last_sources:
                for source in st.session_state.last_sources:
                    source_meta = source.get("source", {})
                    with st.expander(
                        f"Source {source['rank']} - {source_meta.get('file_name', 'Unknown file')} "
                        f"(Chunk {source_meta.get('chunk_number', 'n/a')})"
                    ):
                        st.write(source["chunk"])
            else:
                st.info("Ask a document question to see highlighted source chunks.")

            st.subheader("Page Number Retrieval")
            st.info(
                "Chunk-level source retrieval is active. Exact PDF page numbers require page-aware chunking/OCR metadata."
            )

        with dashboard_tabs[4]:
            st.subheader("Document Privacy")
            st.markdown(
                "- Documents are stored locally in this workspace.\n"
                "- User accounts are stored locally with hashed passwords.\n"
                "- Uploaded document text is only sent to the model as retrieved context when you ask a question."
            )

            st.subheader("OCR Support")
            if processing_stats["scanned"]:
                st.warning(
                    f"{processing_stats['scanned']} document(s) may need OCR because no extractable text was found."
                )
            else:
                st.success("All current documents have extractable text.")

            st.subheader("Streaming Responses")
            st.info(
                "Streaming preference is enabled."
                if st.session_state.streaming_responses
                else "Streaming preference is disabled."
            )

        with dashboard_tabs[5]:
            st.subheader("Graph Visualization")
            graph_col, chart_col = st.columns([1.15, 1])

            with graph_col:
                if embeddings is not None and embeddings.shape[0] > 0:
                    st.markdown(
                        get_vector_graph_html(embeddings),
                        unsafe_allow_html=True,
                    )
                else:
                    st.info("Vectors will appear after documents are processed.")

            with chart_col:
                document_chart_data = get_document_chart_data(documents)

                if document_chart_data:
                    st.caption("Chunks per document")
                    st.bar_chart(
                        document_chart_data,
                        x="File",
                        y="Chunks",
                        height=220,
                    )

                    st.caption("Words per document")
                    st.line_chart(
                        document_chart_data,
                        x="File",
                        y="Words",
                        height=220,
                    )

                vector_similarity_rows = get_vector_similarity_rows(embeddings)
                if vector_similarity_rows:
                    st.caption("Nearest chunk similarity")
                    st.bar_chart(
                        vector_similarity_rows,
                        x="Chunk",
                        y="Similarity",
                        height=220,
                    )
                elif not document_chart_data:
                    st.info("Add documents to see graph-based analytics.")

    with st.expander("Advanced vector details", expanded=False):
        st.markdown("---")
        st.subheader("Knowledge Base Vector Index")
        st.info("FAISS stores semantic embeddings across every document in the knowledge base")
        st.write(f"Total Documents: {len(documents)}")
        st.write(f"Total Chunks Stored: {len(chunks)}")
        st.write(f"Embedding Dimension: {embeddings.shape[1]}")
        st.write(f"Total Embeddings Stored: {embeddings.shape[0]}")
        st.write("### FAISS Index")
        st.write(index)
        st.write("### All Chunks")
        for i, chunk in enumerate(chunks):
            with st.expander(f"Chunk {i+1}"):
                st.write(chunk)
                if st.button("Bookmark chunk", key=f"bookmark_chunk_{i}"):
                    st.session_state.bookmarked_chunks.append(
                        {
                            "chunk_number": i + 1,
                            "chunk": chunk,
                            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                    st.success("Chunk bookmarked")

        if st.session_state.bookmarked_chunks:
            st.write("### Bookmarked Chunks")
            for bookmark in st.session_state.bookmarked_chunks:
                with st.expander(f"Bookmarked chunk {bookmark['chunk_number']} - {bookmark['time']}"):
                    st.write(bookmark["chunk"])

else:
    st.markdown('<div class="info-box"><strong>Upload PDFs</strong> in the sidebar to get started</div>', unsafe_allow_html=True)
