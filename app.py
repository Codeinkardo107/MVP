import os
import json
import yaml
import uuid
import tempfile
import shutil
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, abort, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename
from flask_cors import CORS
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
import io
import requests  # For OpenRouter API calls
import pandas as pd  # For CSV/Excel processing
from docx import Document  # For DOCX generation
from fpdf import FPDF  # For PDF generation

# LangChain imports (still used for text processing)
from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
CORS(app, origins=["http://localhost:8501", "http://127.0.0.1:8501"], 
     methods=["GET", "POST", "OPTIONS"],
     allow_headers=["Content-Type", "Authorization"])

# Configuration
CHUNK_SIZE = 2000
CHUNK_OVERLAP = 400
SUPPORTED_DOC_TYPES = [".pdf", ".docx", ".txt", ".csv", ".xlsx"]
SUPPORTED_CONFIG_TYPES = [".yaml", ".yml", ".json"]
MIN_TEXT_LENGTH = 50
MAX_CONFIG_SIZE = 1 * 1024 * 1024  # 1MB
SESSION_EXPIRE_HOURS = 2

# In-memory session store (replace with Redis in production)
sessions = {}

# OpenRouter configuration
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "anthropic/claude-3-opus"  # Can be changed to any supported model

def allowed_document_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in {'pdf', 'docx', 'txt', 'csv', 'xlsx'}

def allowed_config_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in {'yaml', 'yml', 'json'}

def extract_text_with_ocr(pdf_path):
    """Extract text from PDF with fallback to OCR"""
    doc = fitz.open(pdf_path)
    full_text = ""
    
    for page_num in range(len(doc)):
        try:
            page = doc.load_page(page_num)
            text = page.get_text("text")
            
            if len(text.strip()) < MIN_TEXT_LENGTH:
                pix = page.get_pixmap(dpi=200)
                img = Image.open(io.BytesIO(pix.tobytes("ppm")))
                text = pytesseract.image_to_string(img)
                text = f"[OCR EXTRACTED]\n{text}\n"
                
            full_text += text + "\n\n"
            
        except Exception as e:
            continue
            
    doc.close()
    return full_text.strip()

def parse_config_file(file):
    """Parse and validate config file"""
    try:
        # Check file size
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(0)
        if size > MAX_CONFIG_SIZE:
            raise ValueError(f"Config file exceeds {MAX_CONFIG_SIZE/1024/1024}MB limit")
        
        # Secure temporary file handling
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            file.save(tmp.name)
            with open(tmp.name, 'r') as f:
                if file.filename.lower().endswith('.json'):
                    config = json.load(f)
                else:
                    config = yaml.safe_load(f)
                
                # Validate config structure
                if not isinstance(config, dict):
                    raise ValueError("Config must be a dictionary")
                
                if 'fields' not in config:
                    raise ValueError("Config must contain 'fields' key")
                
                if not isinstance(config['fields'], list):
                    raise ValueError("Fields must be a list")
                
                # Validate each field
                for field in config['fields']:
                    if not isinstance(field, dict):
                        raise ValueError("Each field must be a dictionary")
                    if 'keywords' not in field:
                        raise ValueError("Field missing 'keywords' list")
                
                return config
                
    except (yaml.YAMLError, json.JSONDecodeError) as e:
        raise ValueError(f"Invalid config format: {str(e)}")
    except Exception as e:
        raise ValueError(f"Config processing error: {str(e)}")

def query_openrouter(prompt, session_id=None):
    """Call OpenRouter API with the given prompt"""
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3
    }
    
    try:
        response = requests.post(OPENROUTER_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        raise ValueError(f"OpenRouter API error: {str(e)}")

def build_dynamic_prompt(fields, text):
    """Generate analysis prompt based on fields"""
    fields_section = "\n".join(
        f"- {field.get('name', f'field_{i+1}')}: "
        f"Keywords: {', '.join(field.get('keywords', []))}\n"
        f"  Response type: {field.get('response_type', 'auto')}\n"
        f"  Description: {field.get('description', 'N/A')}"
        for i, field in enumerate(fields)
    )
    
    return f"""Analyze this document and extract information:

FIELDS TO EXTRACT:
{fields_section}

DOCUMENT CONTENT:
{text[:15000]}

INSTRUCTIONS:
1. For each field, determine appropriate response format:
   - Concise: For IDs, numbers, simple facts
   - Detailed: For policies, conditions
2. Return JSON with this structure:
{{
  "results": [
    {{
      "field": "field_name",
      "value": "extracted_value",
      "type": "concise/detailed",
      "confidence": 0.0-1.0
    }}
  ]
}}

OUTPUT:"""

@app.route('/upload_config', methods=['POST'])
def upload_config():
    """First step: Upload configuration file"""
    if 'config_file' not in request.files:
        abort(400, "No config file uploaded")
    
    config_file = request.files['config_file']
    if not config_file.filename or not allowed_config_file(config_file.filename):
        abort(400, "Invalid config file type")
    
    try:
        config = parse_config_file(config_file)
        session_id = str(uuid.uuid4())
        expiry = datetime.now() + timedelta(hours=SESSION_EXPIRE_HOURS)
        
        sessions[session_id] = {
            "config": config,
            "expiry": expiry
        }
        
        return jsonify({
            "status": "success",
            "session_id": session_id,
            "expires_at": expiry.isoformat()
        })
    except ValueError as e:
        abort(400, str(e))

@app.route('/upload_documents', methods=['POST'])
def upload_documents():
    """Second step: Upload documents and process with config"""
    # Validate session
    if 'session_id' not in request.form:
        abort(400, "Session ID required")
    
    session_id = request.form['session_id']
    if session_id not in sessions:
        abort(400, "Invalid or expired session ID")
    
    config = sessions[session_id]['config']
    
    # Validate documents
    if 'document_files' not in request.files:
        abort(400, "No documents uploaded")
    
    document_files = request.files.getlist('document_files')
    if not document_files or all(f.filename == '' for f in document_files):
        abort(400, "No selected files")
    
    # Process documents
    documents = []
    temp_dir = tempfile.mkdtemp()
    
    try:
        for file in document_files:
            if not file or not file.filename or not allowed_document_file(file.filename):
                continue
                
            try:
                filename = secure_filename(file.filename)
                filepath = os.path.join(temp_dir, filename)
                file.save(filepath)
                
                # Process based on file type
                if filename.lower().endswith('.pdf'):
                    try:
                        loader = PyPDFLoader(filepath)
                        docs = loader.load()
                        if sum(len(d.page_content) for d in docs) < MIN_TEXT_LENGTH * len(docs):
                            full_text = extract_text_with_ocr(filepath)
                            if full_text.strip():
                                docs = [Document(page_content=full_text)]
                    except Exception as e:
                        full_text = extract_text_with_ocr(filepath)
                        docs = [Document(page_content=full_text)] if full_text.strip() else []
                        
                    documents.extend(docs)
                    
                elif filename.lower().endswith('.docx'):
                    loader = Docx2txtLoader(filepath)
                    documents.extend(loader.load())
                elif filename.lower().endswith('.txt'):
                    loader = TextLoader(filepath)
                    documents.extend(loader.load())
                elif filename.lower().endswith('.csv'):
                        df = pd.read_csv(filepath)
                        content = df.to_markdown(index=False)
                        print("Extracted CSV content:\n", content[:300])
                        docs = [Document(page_content=content, metadata={"source": filename})]

                elif filename.lower().endswith('.xlsx'):
                    df = pd.read_excel(filepath, engine='openpyxl')
                    # 🧹 Clean: remove fully empty rows/columns
                    df.dropna(how='all', inplace=True)
                    df.dropna(axis=1, how='all', inplace=True)

                    if not df.empty:
                        try:
                            content = df.to_markdown(index=False)
                        except ImportError:
                            content = df.to_string(index=False)

                        print("Extracted Excel content:\n", content[:300])
                        docs = [Document(page_content=content)]
                    else:
                        print("WARNING: Excel sheet is empty after cleaning.")
                        docs = []

                    # Only load if we have a valid loader

                    if docs:
                        documents.extend(docs)
                        print(f"Loaded {len(docs)} documents from {filename}")
                    else:
                        print(f"WARNING: Unsupported file format: {filename}")
                        
                    
            except Exception as e:
                continue
    
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
    
    if not documents:
        abort(400, "No valid content extracted from documents")
    
    # Process text
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP
    )
    splits = text_splitter.split_documents(documents)
    
    # Filter out noise
    filtered_splits = []
    for doc in splits:
        content = doc.page_content.strip()
        if (len(content) > 100 and 
            not content.count('_') > len(content) * 0.3 and
            not content.count('.') > len(content) * 0.1):
            filtered_splits.append(doc)
    
    full_text = "\n\n".join([doc.page_content for doc in filtered_splits])
    
    # Generate and process prompt
    try:
        prompt = build_dynamic_prompt(config['fields'], full_text)
        llm_response = query_openrouter(prompt, session_id)
        
        # Extract JSON from response
        start_idx = llm_response.find('{')
        end_idx = llm_response.rfind('}') + 1
        json_result = llm_response[start_idx:end_idx]
        
        try:
            parsed = json.loads(json_result)
            sessions[session_id]['extraction_results'] = parsed

            return jsonify({
                "status": "success",
                "data": parsed,
                "text_sample": full_text[:500] + "..." if len(full_text) > 500 else full_text
            })
        except json.JSONDecodeError:
            return jsonify({
                "status": "partial_success",
                "raw_response": llm_response,
                "message": "Could not parse LLM response as JSON"
            })
            
    except Exception as e:
        abort(500, f"Analysis failed: {str(e)}")


@app.route('/download/<format>', methods=['GET'])
def download_result(format):
    session_id = request.args.get('session_id')
    if not session_id or session_id not in sessions:
        abort(404, "Invalid or expired session ID")
    
    result_data = sessions[session_id].get('extraction_results')
    if not result_data:
        abort(404, "No results available for this session")

    output = io.BytesIO()

    try:
        if format == 'json':
            output.write(json.dumps(result_data, indent=2).encode('utf-8'))
            o_format = "extracted_data.json"
            mime = "application/json"

        elif format == 'txt':
            text = '\n'.join(f"{item['field']}: {item['value']}" for item in result_data['results'])
            output.write(text.encode('utf-8'))
            o_format = "extracted_data.txt"
            mime = "text/plain"

        elif format == 'xml':
            xml = "<results>\n"
            for item in result_data['results']:
                xml += f"  <field name= '{item['field']}' confidence='{item['confidence']}'>\n"
                xml += f"   <value>{item['value']}</value>\n"
                xml += f"  </field>\n"
            xml += "</results>"
            output.write(xml.encode('utf-8'))
            o_format = "extracted_data.xml"
            mime = "application/xml"

        elif format == 'docx':
            doc = Document()
            doc.add_heading("Extracted Data", level=1)
            for item in result_data['results']:
                doc.add_paragraph(f" {item['field']}: {item['value']}")  # output in format of Key-value pair
            doc.save(output)
            output.seek(0)
            o_format = "extracted_data.docx"
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        
        elif format == 'pdf':
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Arial", size=12)
            for item in result_data['results']:
                pdf.multi_cell(0, 10, f"{item['field']}: {item['value'].encode('latin-1', 'replace').decode('latin-1')}")
            pdf.output(output)
            o_format = "extracted_data.pdf"
            mime = "application/pdf"
        else:
            abort(400, "Unsupported export format")

        output.seek(0)
        return send_file(output, as_attachment=True, download_name=o_format, mimetype=mime)

    except Exception as e:
        abort(500, f"Error generating {format} file: {str(e)}")


@app.route('/session/<session_id>', methods=['GET'])
def get_session(session_id):
    """Check session status"""
    if session_id not in sessions:
        abort(404, "Session not found")
    
    return jsonify({
        "status": "active",
        "expires_at": sessions[session_id]['expiry'].isoformat(),
        "fields": [f['name'] for f in sessions[session_id]['config']['fields']]
    })

@app.route('/')
def home():
    return jsonify({
        "message": "Document Analysis API",
        "endpoints": {
            "/upload_config": "POST - Upload configuration",
            "/upload_documents": "POST - Upload documents with session_id",
            "/session/<id>": "GET - Check session status",
            "/health": "GET - Service health"
        }
    })

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "active_sessions": len(sessions)
    })

if __name__ == '__main__':
    # Verify Tesseract installation
    try:
        pytesseract.get_tesseract_version()
    except EnvironmentError:
        print("Warning: Tesseract OCR not installed")
    
    app.run(host='0.0.0.0', port=5001, debug=True)
