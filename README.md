# Zotero Reference Importer / Zotero 文献导入脚本

## English
This project extracts references from a PDF, enriches missing DOIs via APIs, and imports metadata into Zotero using the Web API. It can reuse an existing `refs.jsonl` to avoid re-parsing and export Google Scholar links for missing DOIs.

**Accuracy note**
- Only Crossref is validated in this project.
- Even with Crossref, DOI matching is not 100% accurate.
- API lookups are only applied when a DOI is missing.
- When only a title is available, Crossref-based DOI matching may be inaccurate.

### Example workflow
1) Extract references and enrich missing DOIs:
```
python run_pipeline.py "C:\path\paper.pdf" --output refs.jsonl --no-zotero --crossref-rows 20 --crossref-threshold 0.75
```
2) Import JSONL into Zotero:
```
python run_pipeline.py --input refs.jsonl --api-key <KEY> --library-type user --library-id <ID> --no-crossref
```
3) Export Google Scholar links for missing DOIs:
```
python run_pipeline.py "C:\path\paper.pdf" --output refs.jsonl --no-zotero --missing-doi-links missing_doi_links.csv
```

## 中文
本项目用于从 PDF 中提取参考文献，调用 API 补全缺失 DOI，并通过 Zotero Web API 导入文献元数据。支持复用已有 `refs.jsonl`，避免重复解析，并可导出缺失 DOI 的 Google Scholar 查询链接。

**准确性说明**
- 当前仅 Crossref 的查询结果经过验证。
- 即使是 Crossref，也无法保证 100% 准确。
- 仅对缺失 DOI 的条目进行 API 查询。
- 当只有标题时，Crossref 的 DOI 匹配可能不准确。

### 示例流程
1) 解析 PDF 并补全缺失 DOI：
```
python run_pipeline.py "C:\path\paper.pdf" --output refs.jsonl --no-zotero --crossref-rows 20 --crossref-threshold 0.75
```
2) 导入 Zotero Web API：
```
python run_pipeline.py --input refs.jsonl --api-key <KEY> --library-type user --library-id <ID> --no-crossref
```
3) 导出缺失 DOI 的 Google Scholar 链接：
```
python run_pipeline.py "C:\path\paper.pdf" --output refs.jsonl --no-zotero --missing-doi-links missing_doi_links.csv
```

## Project Structure / 项目结构
```
downloadPaper/
├─ run_pipeline.py            # main pipeline: parse -> enrich -> export/import
├─ download_test.py           # Crossref query example
├─ prompt                     # original requirements
├─ AGENTS.md                  # repository guidelines
├─ README.md                  # this document
└─ .gitignore                 # ignores local artifacts
```

## Git Ignore / 忽略文件
- The following files are ignored and should not be committed:
  - `refs.jsonl`
  - `missing_doi_links.csv`
  - `references.pdf`
  - `move_pdfs_to_root.py`
  - `zoteroKey`
- Local IDE/venv artifacts are also ignored (e.g., `.idea/`, `.venv/`).
