#!/usr/bin/env python3
"""DocForge / NEXUS FILE STUDIO: local document vault backed by SQLite."""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from pathlib import Path
import cgi
import html
import json
import os
import shutil
import sqlite3
import uuid

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "docforge.sqlite3"
APP_NAME = "NEXUS FILE STUDIO / DocForge"


def ensure_storage():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                stored_name TEXT NOT NULL,
                original_name TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                size INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_title ON documents(title)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_tags ON documents(tags)")


def db_rows(query, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query, params)]


def db_one(query, params=()):
    rows = db_rows(query, params)
    return rows[0] if rows else None


def render_page(content, *, title=APP_NAME):
    return f"""<!doctype html>
<html lang=\"pt-BR\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{html.escape(title)}</title>
  <link rel=\"stylesheet\" href=\"/static/style.css\">
</head>
<body>
  <header class=\"hero\">
    <div>
      <p class=\"eyebrow\">Sistema local com SQLite</p>
      <h1>{APP_NAME}</h1>
      <p>Cadastre, pesquise, baixe e organize arquivos em um cofre local simples.</p>
    </div>
    <nav>
      <a href=\"/\">Arquivos</a>
      <a href=\"/upload\">Novo upload</a>
      <a href=\"/api/documents\">API JSON</a>
    </nav>
  </header>
  <main>{content}</main>
</body>
</html>"""


class DocForgeHandler(BaseHTTPRequestHandler):
    server_version = "DocForge/1.0"

    def send_html(self, content, status=200):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/static/style.css":
            return self.serve_static_css()
        if parsed.path == "/api/documents":
            return self.serve_api_documents(parsed)
        if parsed.path.startswith("/download/"):
            return self.serve_download(parsed.path.rsplit("/", 1)[-1])
        if parsed.path == "/upload":
            return self.serve_upload_form()
        if parsed.path == "/":
            return self.serve_index(parsed)
        self.send_error(404, "Página não encontrada")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            return self.handle_upload()
        if parsed.path.startswith("/delete/"):
            return self.handle_delete(parsed.path.rsplit("/", 1)[-1])
        self.send_error(404, "Ação não encontrada")

    def serve_index(self, parsed):
        query = parse_qs(parsed.query).get("q", [""])[0].strip()
        if query:
            like = f"%{query}%"
            docs = db_rows(
                """
                SELECT * FROM documents
                WHERE title LIKE ? OR description LIKE ? OR tags LIKE ? OR original_name LIKE ?
                ORDER BY updated_at DESC, id DESC
                """,
                (like, like, like, like),
            )
        else:
            docs = db_rows("SELECT * FROM documents ORDER BY updated_at DESC, id DESC")

        cards = "".join(self.document_card(doc) for doc in docs) or "<p class='empty'>Nenhum arquivo cadastrado ainda.</p>"
        content = f"""
<section class=\"panel\">
  <form class=\"search\" method=\"get\" action=\"/\">
    <input name=\"q\" value=\"{html.escape(query)}\" placeholder=\"Buscar por título, tag, descrição ou nome do arquivo\">
    <button type=\"submit\">Pesquisar</button>
    <a class=\"button secondary\" href=\"/upload\">Adicionar arquivo</a>
  </form>
</section>
<section class=\"grid\">{cards}</section>
"""
        self.send_html(render_page(content))

    def document_card(self, doc):
        size = self.human_size(doc["size"])
        return f"""
<article class=\"card\">
  <div class=\"card-top\">
    <h2>{html.escape(doc['title'])}</h2>
    <span>{size}</span>
  </div>
  <p>{html.escape(doc['description']) or 'Sem descrição.'}</p>
  <p class=\"meta\">Arquivo: {html.escape(doc['original_name'])}</p>
  <p class=\"tags\">{html.escape(doc['tags'])}</p>
  <div class=\"actions\">
    <a class=\"button\" href=\"/download/{doc['id']}\">Baixar</a>
    <form method=\"post\" action=\"/delete/{doc['id']}\">
      <button class=\"danger\" type=\"submit\">Excluir</button>
    </form>
  </div>
</article>"""

    def serve_upload_form(self):
        content = """
<section class=\"panel narrow\">
  <h2>Novo documento</h2>
  <form class=\"stack\" method=\"post\" action=\"/upload\" enctype=\"multipart/form-data\">
    <label>Título<input name=\"title\" required maxlength=\"140\"></label>
    <label>Descrição<textarea name=\"description\" rows=\"4\"></textarea></label>
    <label>Tags<input name=\"tags\" placeholder=\"contrato, fiscal, projeto\"></label>
    <label>Arquivo<input name=\"file\" type=\"file\" required></label>
    <button type=\"submit\">Salvar no DocForge</button>
  </form>
</section>"""
        self.send_html(render_page(content, title="Novo upload - DocForge"))

    def handle_upload(self):
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
        file_item = form["file"] if "file" in form else None
        if file_item is None or not file_item.filename:
            return self.send_error(400, "Arquivo obrigatório")
        original = Path(file_item.filename).name
        stored = f"{uuid.uuid4().hex}_{original}"
        target = UPLOAD_DIR / stored
        with target.open("wb") as output:
            shutil.copyfileobj(file_item.file, output)
        title = (form.getfirst("title") or original).strip()[:140]
        description = (form.getfirst("description") or "").strip()
        tags = (form.getfirst("tags") or "").strip()
        content_type = file_item.type or "application/octet-stream"
        size = target.stat().st_size
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO documents (title, description, tags, stored_name, original_name, content_type, size, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (title, description, tags, stored, original, content_type, size),
            )
        self.redirect("/")

    def handle_delete(self, doc_id):
        doc = db_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
        if doc:
            (UPLOAD_DIR / doc["stored_name"]).unlink(missing_ok=True)
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        self.redirect("/")

    def serve_download(self, doc_id):
        doc = db_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
        if not doc:
            return self.send_error(404, "Documento não encontrado")
        path = UPLOAD_DIR / doc["stored_name"]
        if not path.exists():
            return self.send_error(410, "Arquivo ausente no disco")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", doc["content_type"])
        self.send_header("Content-Disposition", f"attachment; filename=\"{doc['original_name']}\"")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_api_documents(self, parsed):
        query = parse_qs(parsed.query).get("q", [""])[0].strip()
        docs = db_rows("SELECT * FROM documents ORDER BY updated_at DESC, id DESC")
        if query:
            q = query.lower()
            docs = [d for d in docs if q in json.dumps(d, ensure_ascii=False).lower()]
        body = json.dumps({"documents": docs}, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_static_css(self):
        css = (BASE_DIR / "static" / "style.css").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(css)))
        self.end_headers()
        self.wfile.write(css)

    @staticmethod
    def human_size(size):
        value = float(size)
        for unit in ["B", "KB", "MB", "GB"]:
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024


def main():
    ensure_storage()
    host = os.environ.get("DOCFORGE_HOST", "127.0.0.1")
    port = int(os.environ.get("DOCFORGE_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), DocForgeHandler)
    print(f"{APP_NAME} disponível em http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
