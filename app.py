import streamlit as st
import requests
from io import BytesIO
import traceback
import xml.etree.ElementTree as ET
import json
import time

# Configuration
BACKEND_URL = "http://127.0.0.1:5001"  # Update if your backend runs on a different port

st.set_page_config(page_title="Document Analyzer", layout="wide")
st.title("📄 Advanced Document Analyzer")
st.markdown("""
    **Two-step document analysis:**  
    1. First upload your configuration file  
    2. Then upload documents for analysis  
""")

# Initialize session state variables
if 'session_id' not in st.session_state:
    st.session_state.session_id = None
if 'config_uploaded' not in st.session_state:
    st.session_state.config_uploaded = False

# Sidebar for configuration
with st.sidebar:
    st.header("Workflow")
    st.markdown("""
    **How to use:**
    1. 📝 Upload config file (YAML/JSON)
    2. 📂 Upload documents (PDF/DOCX/TXT/CSV/XLSX)
    3. 🔍 Analyze documents
    4. 📥 Download results
    """)
    
    if st.session_state.config_uploaded:
        st.success("✅ Config file uploaded")
        if st.button("Clear Session"):
            st.session_state.clear()
            st.rerun()

# Step 1: Config File Upload
st.write("")
st.subheader("Step 1: Upload Configuration File")
config_file = st.file_uploader(
    "Choose a config file (YAML or JSON)",
    type=["yaml", "yml", "json"],
    key="config_upload",
    disabled=st.session_state.config_uploaded
)

if config_file and not st.session_state.config_uploaded:
    if st.button("Upload Configuration"):
        with st.spinner("Processing config..."):
            try:
                files = {"config_file": (config_file.name, config_file.getvalue())}
                response = requests.post(
                    f"{BACKEND_URL}/upload_config",
                    files=files,
                    timeout=10
                )
                
                if response.status_code == 200:
                    st.session_state.session_id = response.json().get("session_id")
                    st.session_state.config_uploaded = True
                    st.success("Configuration uploaded successfully!")
                    st.rerun()
                else:
                    st.error(f"Error: {response.text}")
                    
            except requests.exceptions.ConnectionError:
                st.error("Cannot connect to backend. Make sure the Flask app is running.")
            except Exception as e:
                st.error(f"An error occurred: {str(e)}")
                st.code(traceback.format_exc())

# Step 2: Document Upload and Processing
if st.session_state.config_uploaded:
    st.write("")
    st.write("")
    st.subheader("Step 2: Upload Documents for Analysis")
    uploaded_files = st.file_uploader(
        "Choose documents to analyze",
        type=["pdf", "docx", "txt", "csv", "xlsx"],
        accept_multiple_files=True,
        key="doc_upload"
    )
    
    if uploaded_files:
        if st.button("Analyze Documents"):
            with st.spinner("Processing documents..."):
                try:
                    files = [("document_files", (file.name, file.getvalue())) for file in uploaded_files]
                    data = {"session_id": st.session_state.session_id}
                    
                    response = requests.post(
                        f"{BACKEND_URL}/upload_documents",
                        files=files,
                        data=data,
                        timeout=30
                    )
                    
                    if response.status_code == 200:
                        result = response.json()
                        st.session_state.extraction_results = result.get("data", {})
                        st.session_state.text_sample = result.get("text_sample", "")
                        st.success("Analysis complete!")
                    else:
                        st.error(f"Error: {response.text}")
                
                except requests.exceptions.ConnectionError:
                    st.error("Cannot connect to backend. Make sure the Flask app is running.")
                except Exception as e:
                    st.error(f"An error occurred: {str(e)}")
                    st.code(traceback.format_exc())



def download_from_backend(format):
    try:
        session_id = st.session_state.get("session_id")
        if not session_id:
            st.error("Session ID missing. Please upload config and analyze a document first.")
            return None
        
        res = requests.get(f"{BACKEND_URL}/download/{format}", params={"session_id": session_id})
        if res.status_code == 200:
            return res.content
        else:
            st.error(f"Export failed: {res.text}")
    except Exception as e:
        st.error(f"Error downloading {format} file: {str(e)}")
        return None


# Display results if available
if 'extraction_results' in st.session_state:
    st.write("")
    st.write("")
    st.subheader("Analysis Results")
    
    # Show text sample preview
    with st.expander("📝 View Document Sample", expanded=False):
        st.text_area("Extracted Text Sample", 
                    st.session_state.text_sample, 
                    height=200)
    
    # Results display format
    st.write("")
    st.write("")
    st.subheader("Output Options")
    format_col1, format_col2 = st.columns(2)
    
    with format_col1:
        output_format = st.radio(
            "Select output format:",
            ["JSON", "Text", "XML"],
            horizontal=True
        )
    
    with format_col2:
        if st.button("Refresh Results"):
            st.rerun()
    
    # Display results in selected format
    if output_format == "JSON":
        st.json(st.session_state.extraction_results)
    elif output_format == "Text":
        if "results" in st.session_state.extraction_results:
            for item in st.session_state.extraction_results["results"]:
                st.markdown(f"### {item.get('field', 'N/A')}")
                st.markdown(f"**Type:** {item.get('type', 'N/A')}")
                st.markdown(f"**Confidence:** {item.get('confidence', 'N/A')}")
                st.markdown("**Value:**")
                st.write(item.get('value', 'No value extracted'))
                st.divider()
    elif output_format == "XML":
        try:
            root = ET.Element("AnalysisResults")
            if "results" in st.session_state.extraction_results:
                for item in st.session_state.extraction_results["results"]:
                    result_elem = ET.SubElement(root, "Result")
                    ET.SubElement(result_elem, "Field").text = str(item.get('field', ''))
                    ET.SubElement(result_elem, "Type").text = str(item.get('type', ''))
                    ET.SubElement(result_elem, "Confidence").text = str(item.get('confidence', ''))
                    ET.SubElement(result_elem, "Value").text = str(item.get('value', ''))
            
            xml_str = ET.tostring(root, encoding='unicode')
            st.code(xml_str, language="xml")
        except Exception as e:
            st.error(f"Error generating XML: {str(e)}")

    # Download options
    st.write("")
    st.write("")
    st.subheader("📥 Download Results")
    file_data = None
    filename = ""
    mime = ""

    with st.expander("Download Options", expanded=True):
        dl_col1, dl_col2 = st.columns(2)

        with dl_col1:
            if st.button("Download as JSON"):
                file_data = download_from_backend("json")
                if file_data:
                    mime = "application/json"
                    filename = "extraction_results.json"
            if st.button("Download as Text"):
                file_data = download_from_backend("txt")
                if file_data:
                    mime = "text/plain"
                    filename = "extraction_results.txt"

            if st.button("Download as XML"):
                file_data = download_from_backend("xml")
                if file_data:
                    mime = "application/xml"
                    filename = "extraction_results.xml"

            if st.button("Download as DOCX"):
                file_data = download_from_backend("docx")
                if file_data:
                    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    filename = "extraction_results.docx"

            if st.button("Download as PDF"):
                file_data = download_from_backend("pdf")
                if file_data:
                    mime = "application/pdf"
                    filename = "extraction_results.pdf"

        with dl_col2:
            if file_data:
                st.download_button("Click to save", file_data, filename, mime=mime)

# Session info footer
if st.session_state.config_uploaded:
    st.markdown("---")
    st.caption(f"Active session: `{st.session_state.session_id}`")

st.markdown("---")
st.caption("Document Analyzer - Powered by OpenRouter AI")
