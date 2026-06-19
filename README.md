# DocForge

DocForge é um sistema local para PC focado em documentos, imagens, PDFs e ZIPs. Ele roda no navegador, usa SQLite local e mantém arquivos no disco da própria máquina.

## Recursos

- Dashboard com cards para todas as ferramentas principais.
- Upload, busca, download e exclusão de documentos locais.
- Renomeação de imagens por nome base em pastas e dentro de ZIPs, sempre com backup antes de alterar ZIPs.
- Conversão de PDFs em ZIPs individuais, com opção para apagar o PDF original apenas após sucesso.
- Extração de ZIPs para pastas com o mesmo nome.
- Inserção de imagens em ZIPs, com backup automático.
- Organização de arquivos por tipo: `imagens`, `pdfs`, `zips`, `documentos` e `outros`.
- Limpeza de nomes de arquivos: espaços viram underline, caracteres estranhos são removidos e nomes ficam em minúsculo.
- Histórico completo de operações em SQLite, com detalhes por arquivo.
- Relatórios `.txt` gerados em `logs/` e também salvos no banco.
- Presets e configurações locais.
- Interface escura, responsiva, com sidebar e cards glassmorphism.

## Banco SQLite

O banco é criado automaticamente em `data/docforge.sqlite3` com as tabelas:

- `documents`
- `operations`
- `operation_files`
- `settings`
- `presets`
- `reports`

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

- `app.py`: servidor HTTP, rotas, persistência SQLite e operações de arquivos.
- `static/style.css`: interface web premium e responsiva.
- `data/uploads/`: arquivos enviados localmente.
- `logs/`: relatórios de operações.
