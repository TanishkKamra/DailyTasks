# DailyTasks

A LAN document-processing suite for an insurance brokerage's operations team. One Flask server with a blueprint per tool, a PySide6 control panel to run it, and a browser UI that anyone on the office network can open.

## Tools

| Tool | What it does |
|---|---|
| **Policy Reader** | Identifies the insurer of a multi-page policy PDF and extracts premiums, dates, business type and customer details into a standardized Excel sheet. Insurer-specific rules for ZUNO, HDFC ERGO, TATA AIG, GO DIGIT and ICICI Lombard. Text is read with PyMuPDF; an LLM endpoint (Azure AI Foundry, Azure OpenAI, OpenAI-compatible) fills fields the rules can't. |
| **PAN OCR** | Reads PAN card images and PDFs on a shared RapidOCR/ONNX pipeline with orientation correction and label-anchored field extraction. |
| **Aadhaar OCR** | Extracts the 12-digit Aadhaar number and holder name from photos and PDFs, scanned or with a text layer. |
| **LP DateWise (call sampler)** | Stratified random sampling of monthly call records, spread evenly across every 15-minute window of every active hour, with a fixed seed and shortfall redistribution. |
| **LP Delete (sheet flagger)** | Flags .xlsx rows by keyword by editing the workbook's XML directly, so formatting and formulas survive and large files never load into memory. Falls back to openpyxl and verifies the result. |
| **File Sharing** | Upload and organize files over the LAN, or register a folder already on a PC and browse/download it live without copying. |
| **Dashboard** | Recent jobs, request logs and system activity across all tools. |

## Run it

Needs Python 3.10+ on Windows (Linux/macOS scripts included).

```
install.bat          # creates .venv and installs requirements
start.bat            # starts the server on http://0.0.0.0:5000
Control_Panel.bat    # optional PySide6 control panel
```

Copy `.env.example` to `.env` to set the host, port and LLM endpoint for the Policy Reader. Production runs under waitress (or IIS).

## Layout

`app.py` (Flask app factory), `routes/` (one blueprint per tool), `core/` (OCR, policy and Excel engines), `templates/` + `static/` (UI), `server_gui.py` (control panel).

## License

MIT, see [LICENSE](LICENSE).
