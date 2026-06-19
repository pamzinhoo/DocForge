# DocForge — NEXUS FILE STUDIO

Sistema local para organizar documentos com armazenamento em SQLite e arquivos no disco.

## Recursos

- Upload de documentos pelo navegador.
- Cadastro de título, descrição e tags.
- Busca por título, descrição, tags ou nome original do arquivo.
- Download e exclusão de documentos.
- API JSON em `/api/documents`.
- Banco SQLite criado automaticamente em `data/docforge.sqlite3`.

## Como executar

```bash
python3 app.py
```

Acesse `http://127.0.0.1:8000`.

Variáveis opcionais:

```bash
DOCFORGE_HOST=0.0.0.0 DOCFORGE_PORT=8080 python3 app.py
```

## Estrutura

- `app.py`: servidor HTTP, rotas, persistência SQLite e gerenciamento de uploads.
- `static/style.css`: interface web responsiva.
- `data/uploads/`: arquivos enviados localmente (ignorado pelo Git).
