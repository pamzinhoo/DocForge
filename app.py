#!/usr/bin/env python3
"""DocForge: sistema local premium para documentos, imagens, PDFs e ZIPs."""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
import html
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
import zipfile

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "docforge.sqlite3"
APP_NAME = "DocForge"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
DOC_EXTS = {".doc", ".docx", ".odt", ".txt", ".rtf", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".md"}
PDF_EXTS = {".pdf"}
ZIP_EXTS = {".zip"}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def ensure_storage():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL,
                folder_path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                found_count INTEGER NOT NULL DEFAULT 0,
                changed_count INTEGER NOT NULL DEFAULT 0,
                ignored_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL DEFAULT '[]',
                duration_seconds REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operation_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id INTEGER NOT NULL,
                original_path TEXT NOT NULL DEFAULT '',
                final_path TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(operation_id) REFERENCES operations(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id INTEGER NOT NULL,
                report_path TEXT NOT NULL,
                report_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(operation_id) REFERENCES operations(id)
            )
            """
        )


def db_rows(query, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query, params)]


def db_one(query, params=()):
    rows = db_rows(query, params)
    return rows[0] if rows else None


def safe_path(raw):
    path = Path((raw or "").strip()).expanduser()
    if not raw or not path.exists() or not path.is_dir():
        raise ValueError("Informe uma pasta local existente.")
    return path.resolve()


def unique_path(target):
    if not target.exists():
        return target
    i = 1
    while True:
        candidate = target.with_name(f"{target.stem}_{i}{target.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def clean_filename(name):
    stem, suffix = Path(name).stem, Path(name).suffix.lower()
    stem = stem.strip().lower().replace(" ", "_")
    stem = re.sub(r"[^a-z0-9._-]+", "", stem)
    stem = re.sub(r"_+", "_", stem).strip("._-") or "arquivo"
    return stem + suffix


class OperationLog:
    def __init__(self, operation_type, folder_path):
        self.operation_type = operation_type
        self.folder_path = str(folder_path)
        self.files = []
        self.errors = []
        self.found = 0
        self.changed = 0
        self.ignored = 0
        self.started = time.time()

    def add(self, original, final, action, status="ok", error=""):
        self.files.append((str(original), str(final), action, status, error))
        if status == "ok" and action not in {"ignored", "found"}:
            self.changed += 1
        elif status == "ignored" or action == "ignored":
            self.ignored += 1
        elif status == "error":
            self.errors.append(error or str(original))

    def save(self, message="Operação concluída"):
        duration = round(time.time() - self.started, 3)
        status = "error" if self.errors and not self.changed else ("partial" if self.errors else "success")
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                """
                INSERT INTO operations (operation_type, folder_path, status, message, found_count,
                changed_count, ignored_count, error_count, errors_json, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (self.operation_type, self.folder_path, status, message, self.found, self.changed,
                 self.ignored, len(self.errors), json.dumps(self.errors, ensure_ascii=False), duration),
            )
            op_id = cur.lastrowid
            conn.executemany(
                """
                INSERT INTO operation_files (operation_id, original_path, final_path, action, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(op_id, *item) for item in self.files],
            )
        report_text = self.report_text(op_id, status, message, duration)
        report_path = LOG_DIR / f"operation_{op_id}_{self.operation_type}.txt"
        report_path.write_text(report_text, encoding="utf-8")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO reports (operation_id, report_path, report_text) VALUES (?, ?, ?)", (op_id, str(report_path), report_text))
        return op_id

    def report_text(self, op_id, status, message, duration):
        lines = [f"DocForge - Relatório #{op_id}", f"Tipo: {self.operation_type}", f"Pasta: {self.folder_path}",
                 f"Status: {status}", f"Mensagem: {message}", f"Encontrados: {self.found}",
                 f"Alterados: {self.changed}", f"Ignorados: {self.ignored}", f"Erros: {len(self.errors)}",
                 f"Duração: {duration}s", "", "Arquivos:"]
        lines += [f"- [{st}] {act}: {orig} -> {final} {err}" for orig, final, act, st, err in self.files]
        return "\n".join(lines) + "\n"


def backup_zip(zip_path):
    backup = zip_path.with_suffix(zip_path.suffix + f".bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(zip_path, backup)
    return backup


def rename_images(folder, base):
    log = OperationLog("rename_images", folder)
    base = base.strip()
    targets = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"} and p.stem == base]
    log.found += len(targets)
    n = 1
    for src in targets:
        dst = unique_path(src.with_name(f"{base}{n}{src.suffix.lower()}")); n += 1
        try:
            src.rename(dst); log.add(src, dst, "rename")
        except Exception as exc: log.add(src, src, "rename", "error", str(exc))
    for z in folder.glob("*.zip"):
        log.found += 1
        try:
            backup_zip(z)
            with zipfile.ZipFile(z, "r") as zin:
                infos = zin.infolist(); data = {i.filename: zin.read(i.filename) for i in infos if not i.is_dir()}
            renamed = []
            with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zout:
                idx = 1
                for name, content in data.items():
                    p = Path(name)
                    new_name = name
                    if p.suffix.lower() in {".png", ".jpg", ".jpeg"} and p.stem == base:
                        new_name = str(p.with_name(f"{base}{idx}{p.suffix.lower()}")); idx += 1; renamed.append((name, new_name))
                    zout.writestr(new_name, content)
            for old, new in renamed: log.add(f"{z}!{old}", f"{z}!{new}", "rename_in_zip")
            if not renamed: log.add(z, z, "ignored", "ignored", "Nenhuma imagem alvo no ZIP")
        except Exception as exc: log.add(z, z, "zip_rename", "error", str(exc))
    return log.save()


def pdf_to_zip(folder, delete_pdf=False):
    log = OperationLog("pdf_to_zip", folder)
    pdfs = list(folder.glob("*.pdf")); log.found = len(pdfs)
    for pdf in pdfs:
        try:
            zp = unique_path(pdf.with_suffix(".zip"))
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z: z.write(pdf, pdf.name)
            log.add(pdf, zp, "convert_pdf_to_zip")
            if delete_pdf: pdf.unlink(); log.add(pdf, zp, "delete_original_pdf")
        except Exception as exc: log.add(pdf, pdf, "convert_pdf_to_zip", "error", str(exc))
    return log.save()


def extract_zips(folder):
    log = OperationLog("extract_zips", folder); zips = list(folder.glob("*.zip")); log.found = len(zips)
    for z in zips:
        try:
            out = unique_path(folder / z.stem); out.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(z) as zipf: zipf.extractall(out)
            log.add(z, out, "extract")
        except Exception as exc: log.add(z, z, "extract", "error", str(exc))
    return log.save()


def insert_images_in_zips(zip_folder, image_folder):
    log = OperationLog("insert_images_in_zips", zip_folder)
    zips = list(zip_folder.glob("*.zip")); images = [p for p in image_folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    log.found = len(zips) + len(images)
    for z in zips:
        try:
            backup_zip(z)
            with zipfile.ZipFile(z, "a", zipfile.ZIP_DEFLATED) as zipf:
                existing = set(zipf.namelist())
                for img in images:
                    arc = img.name if img.name not in existing else f"images/{img.name}"
                    zipf.write(img, arc); log.add(img, f"{z}!{arc}", "insert_image_in_zip")
        except Exception as exc: log.add(z, z, "insert_image_in_zip", "error", str(exc))
    return log.save()


def organize_files(folder):
    log = OperationLog("organize_files", folder)
    buckets = {"imagens": IMAGE_EXTS, "pdfs": PDF_EXTS, "zips": ZIP_EXTS, "documentos": DOC_EXTS}
    files = [p for p in folder.iterdir() if p.is_file()]; log.found = len(files)
    for f in files:
        bucket = next((name for name, exts in buckets.items() if f.suffix.lower() in exts), "outros")
        try:
            dest_dir = folder / bucket; dest_dir.mkdir(exist_ok=True)
            dest = unique_path(dest_dir / f.name); shutil.move(str(f), str(dest)); log.add(f, dest, "organize")
        except Exception as exc: log.add(f, f, "organize", "error", str(exc))
    return log.save()


def clean_names(folder):
    log = OperationLog("clean_names", folder); files = [p for p in folder.iterdir() if p.is_file()]; log.found = len(files)
    for f in files:
        new = clean_filename(f.name)
        if new == f.name: log.add(f, f, "ignored", "ignored", "Nome já padronizado"); continue
        try:
            dest = unique_path(f.with_name(new)); f.rename(dest); log.add(f, dest, "clean_name")
        except Exception as exc: log.add(f, f, "clean_name", "error", str(exc))
    return log.save()


def esc(value):
    return html.escape(str(value or ""))


def render_page(content, *, title=APP_NAME, active="dashboard"):
    items = [("/", "dashboard", "Dashboard"), ("/upload", "upload", "Upload"), ("/history", "history", "Histórico"), ("/presets", "presets", "Presets"), ("/settings", "settings", "Configurações")]
    nav = "".join(f'<a class="{ "active" if key == active else ""}" href="{href}">{label}</a>' for href, key, label in items)
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title><link rel="stylesheet" href="/static/style.css"></head><body><aside class="sidebar"><div class="brand"><span>◆</span><strong>DocForge</strong><small>Local File OS</small></div><nav>{nav}</nav></aside><main class="shell">{content}</main></body></html>"""


class DocForgeHandler(BaseHTTPRequestHandler):
    server_version = "DocForge/2.0"

    def send_html(self, content, status=200):
        body = content.encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def redirect(self, location):
        self.send_response(303); self.send_header("Location", location); self.end_headers()

    def post_data(self):
        length = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(length).decode("utf-8")
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/static/style.css": return self.serve_static_css()
        if parsed.path == "/api/documents": return self.serve_api_documents(parsed)
        if parsed.path.startswith("/download/"): return self.serve_download(parsed.path.rsplit("/", 1)[-1])
        routes = {"/": self.serve_dashboard, "/upload": self.serve_upload_form, "/history": self.serve_history, "/presets": self.serve_presets, "/settings": self.serve_settings}
        if parsed.path.startswith("/operation/"): return self.serve_operation(parsed.path.rsplit("/", 1)[-1])
        if parsed.path in routes: return routes[parsed.path](parsed)
        self.send_error(404, "Página não encontrada")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/upload": return self.handle_upload()
        if parsed.path.startswith("/delete/"): return self.handle_delete(parsed.path.rsplit("/", 1)[-1])
        if parsed.path == "/run": return self.handle_run()
        if parsed.path == "/settings": return self.handle_settings()
        if parsed.path == "/presets": return self.handle_presets()
        if parsed.path == "/history/clear":
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("DELETE FROM operation_files"); conn.execute("DELETE FROM reports"); conn.execute("DELETE FROM operations")
            return self.redirect("/history")
        self.send_error(404, "Ação não encontrada")

    def hero(self):
        return """<section class="hero"><p class="eyebrow">Sistema local completo</p><h1>DocForge</h1><p>Automatize imagens, PDFs, ZIPs e documentos no seu PC com histórico, presets e relatórios auditáveis.</p></section>"""

    def serve_dashboard(self, parsed):
        cards = [("rename_images", "Renomear imagens", "Renomeia arquivos base e imagens dentro de ZIPs com backup."), ("insert_images_in_zips", "Inserir imagens em ZIP", "Adiciona imagens em todos os ZIPs de uma pasta."), ("extract_zips", "Extrair ZIP", "Extrai ZIPs para pastas com o mesmo nome."), ("pdf_to_zip", "Converter PDF em ZIP", "Compacta cada PDF em um ZIP individual."), ("organize_files", "Organizar arquivos por tipo", "Cria imagens, pdfs, zips, documentos e outros."), ("clean_names", "Limpar nomes de arquivos", "Remove caracteres estranhos, espaços e padroniza minúsculas."), ("history", "Histórico de operações", "Consulte resultados, erros e relatórios."), ("presets", "Presets", "Salve configurações reutilizáveis."), ("settings", "Configurações", "Ajuste preferências locais.")]
        html_cards = "".join(f'<article class="tool-card"><h2>{esc(t)}</h2><p>{esc(d)}</p><a class="button" href="/{k if k in {"history","presets","settings"} else "#"}" onclick="selectTool(\'{k}\')">Abrir</a></article>' for k,t,d in cards)
        forms = self.operation_forms()
        self.send_html(render_page(self.hero() + f'<section class="grid cards">{html_cards}</section>{forms}<script>function selectTool(id){{let el=document.getElementById(id); if(el) el.scrollIntoView({{behavior:"smooth",block:"center"}});}}</script>', active="dashboard"))

    def operation_forms(self):
        def form(op, title, fields):
            return f'<section id="{op}" class="panel"><h2>{title}</h2><form class="stack" method="post" action="/run"><input type="hidden" name="operation" value="{op}">{fields}<button type="submit">Executar</button></form></section>'
        folder = '<label>Pasta local<input name="folder" placeholder="/caminho/da/pasta" required></label>'
        return '<div class="forms">' + form("rename_images", "Renomear imagens", folder + '<label>Nome base<input name="base_name" placeholder="foto_padrao" required></label>') + form("pdf_to_zip", "Converter PDF em ZIP", folder + '<label class="check"><input type="checkbox" name="delete_pdf" value="1"> Apagar PDF original se o ZIP for criado</label>') + form("extract_zips", "Extrair ZIPs", folder) + form("insert_images_in_zips", "Inserir imagens em ZIPs", '<label>Pasta dos ZIPs<input name="zip_folder" required></label><label>Pasta das imagens<input name="image_folder" required></label>') + form("organize_files", "Organizar arquivos por tipo", folder) + form("clean_names", "Limpar nomes de arquivos", folder) + '</div>'

    def handle_run(self):
        data = self.post_data(); op = data.get("operation")
        try:
            if op == "rename_images": op_id = rename_images(safe_path(data.get("folder")), data.get("base_name", ""))
            elif op == "pdf_to_zip": op_id = pdf_to_zip(safe_path(data.get("folder")), data.get("delete_pdf") == "1")
            elif op == "extract_zips": op_id = extract_zips(safe_path(data.get("folder")))
            elif op == "insert_images_in_zips": op_id = insert_images_in_zips(safe_path(data.get("zip_folder")), safe_path(data.get("image_folder")))
            elif op == "organize_files": op_id = organize_files(safe_path(data.get("folder")))
            elif op == "clean_names": op_id = clean_names(safe_path(data.get("folder")))
            else: raise ValueError("Operação inválida")
            self.redirect(f"/operation/{op_id}")
        except Exception as exc:
            self.send_html(render_page(f'<section class="panel"><h2>Erro</h2><p>{esc(exc)}</p><a class="button" href="/">Voltar</a></section>'), 400)

    def serve_history(self, parsed):
        ops = db_rows("SELECT * FROM operations ORDER BY id DESC LIMIT 200")
        rows = "".join(f'<tr><td>#{o["id"]}</td><td>{esc(o["operation_type"])}</td><td>{esc(o["status"])}</td><td>{o["found_count"]}</td><td>{o["changed_count"]}</td><td>{o["error_count"]}</td><td>{esc(o["created_at"])}</td><td><a href="/operation/{o["id"]}">Detalhes</a></td></tr>' for o in ops) or '<tr><td colspan="8">Sem histórico.</td></tr>'
        content = '<section class="panel"><h1>Histórico</h1><form method="post" action="/history/clear"><button class="danger">Apagar histórico</button></form><div class="table"><table><thead><tr><th>ID</th><th>Tipo</th><th>Status</th><th>Achados</th><th>Alterados</th><th>Erros</th><th>Data</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div></section>'
        self.send_html(render_page(content, active="history"))

    def serve_operation(self, op_id):
        op = db_one("SELECT * FROM operations WHERE id=?", (op_id,)); files = db_rows("SELECT * FROM operation_files WHERE operation_id=? ORDER BY id", (op_id,)); report = db_one("SELECT * FROM reports WHERE operation_id=?", (op_id,))
        if not op: return self.send_error(404, "Operação não encontrada")
        rows = "".join(f'<tr><td>{esc(f["action"])}</td><td>{esc(f["status"])}</td><td>{esc(f["original_path"])}</td><td>{esc(f["final_path"])}</td><td>{esc(f["error_message"])}</td></tr>' for f in files)
        content = f'<section class="panel"><h1>Operação #{op_id}</h1><p>{esc(op["message"])} • {esc(op["status"])} • relatório: {esc(report["report_path"] if report else "")}</p><div class="stats"><span>Encontrados {op["found_count"]}</span><span>Alterados {op["changed_count"]}</span><span>Ignorados {op["ignored_count"]}</span><span>Erros {op["error_count"]}</span></div><div class="table"><table><thead><tr><th>Ação</th><th>Status</th><th>Original</th><th>Final</th><th>Erro</th></tr></thead><tbody>{rows}</tbody></table></div></section>'
        self.send_html(render_page(content, active="history"))

    def serve_presets(self, parsed):
        presets = db_rows("SELECT * FROM presets ORDER BY updated_at DESC")
        rows = "".join(f'<li><strong>{esc(p["name"])}</strong> — {esc(p["operation_type"])} <code>{esc(p["config_json"])}</code></li>' for p in presets) or '<li>Nenhum preset.</li>'
        content = '<section class="panel"><h1>Presets</h1><form class="stack" method="post" action="/presets"><label>Nome<input name="name" required></label><label>Tipo de operação<input name="operation_type" required></label><label>Config JSON<textarea name="config_json">{}</textarea></label><button>Salvar preset</button></form><ul class="list">' + rows + '</ul></section>'
        self.send_html(render_page(content, active="presets"))

    def handle_presets(self):
        d = self.post_data();
        with sqlite3.connect(DB_PATH) as conn: conn.execute("INSERT INTO presets (name, operation_type, config_json, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)", (d.get("name",""), d.get("operation_type",""), d.get("config_json","{}")))
        self.redirect("/presets")

    def serve_settings(self, parsed):
        settings = db_rows("SELECT * FROM settings ORDER BY key")
        rows = "".join(f'<li><strong>{esc(s["key"])}</strong>: {esc(s["value"])}</li>' for s in settings) or '<li>Nenhuma configuração.</li>'
        content = '<section class="panel"><h1>Configurações</h1><form class="stack" method="post" action="/settings"><label>Chave<input name="key" required></label><label>Valor<input name="value"></label><button>Salvar</button></form><ul class="list">' + rows + '</ul></section>'
        self.send_html(render_page(content, active="settings"))

    def handle_settings(self):
        d = self.post_data()
        with sqlite3.connect(DB_PATH) as conn: conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP", (d.get("key",""), d.get("value","")))
        self.redirect("/settings")

    def serve_upload_form(self, parsed=None):
        content = '<section class="panel narrow"><h1>Novo documento</h1><form class="stack" method="post" action="/upload" enctype="multipart/form-data"><label>Título<input name="title" required maxlength="140"></label><label>Descrição<textarea name="description" rows="4"></textarea></label><label>Tags<input name="tags" placeholder="contrato, fiscal, projeto"></label><label>Arquivo<input name="file" type="file" required></label><button type="submit">Salvar no DocForge</button></form></section>'
        self.send_html(render_page(content, title="Novo upload - DocForge", active="upload"))

    def parse_multipart_form(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        header = f"Content-Type: {self.headers.get('Content-Type', '')}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        message = BytesParser(policy=email_policy).parsebytes(header + body)
        fields, files = {}, {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[name] = {"filename": filename, "content_type": part.get_content_type(), "data": payload}
            elif name:
                fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return fields, files

    def handle_upload(self):
        fields, files = self.parse_multipart_form()
        file_item = files.get("file")
        if not file_item or not file_item.get("filename"):
            return self.send_error(400, "Arquivo obrigatório")
        original = Path(file_item["filename"]).name
        stored = f"{uuid.uuid4().hex}_{original}"
        target = UPLOAD_DIR / stored
        target.write_bytes(file_item["data"])
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO documents (title, description, tags, stored_name, original_name, content_type, size, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)", ((fields.get("title") or original).strip()[:140], (fields.get("description") or "").strip(), (fields.get("tags") or "").strip(), stored, original, file_item.get("content_type") or "application/octet-stream", target.stat().st_size))
        self.redirect("/upload")

    def handle_delete(self, doc_id):
        doc = db_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
        if doc:
            (UPLOAD_DIR / doc["stored_name"]).unlink(missing_ok=True)
            with sqlite3.connect(DB_PATH) as conn: conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        self.redirect("/")

    def serve_download(self, doc_id):
        doc = db_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
        if not doc: return self.send_error(404, "Documento não encontrado")
        path = UPLOAD_DIR / doc["stored_name"]
        if not path.exists(): return self.send_error(410, "Arquivo ausente no disco")
        data = path.read_bytes(); self.send_response(200); self.send_header("Content-Type", doc["content_type"]); self.send_header("Content-Disposition", f"attachment; filename=\"{doc['original_name']}\""); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def serve_api_documents(self, parsed):
        docs = db_rows("SELECT * FROM documents ORDER BY updated_at DESC, id DESC")
        body = json.dumps({"documents": docs}, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def serve_static_css(self):
        css = (BASE_DIR / "static" / "style.css").read_bytes()
        self.send_response(200); self.send_header("Content-Type", "text/css; charset=utf-8"); self.send_header("Content-Length", str(len(css))); self.end_headers(); self.wfile.write(css)


def main():
    ensure_storage(); host = os.environ.get("DOCFORGE_HOST", "127.0.0.1"); port = int(os.environ.get("DOCFORGE_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), DocForgeHandler)
    print(f"{APP_NAME} disponível em http://{host}:{port}"); server.serve_forever()


if __name__ == "__main__":
    main()
