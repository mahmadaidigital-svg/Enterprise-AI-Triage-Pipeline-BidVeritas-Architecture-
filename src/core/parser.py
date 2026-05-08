import fitz  # PyMuPDF
import docx  # python-docx
import openpyxl  # openpyxl
import pandas as pd  # pandas
import logging
import hashlib
from pathlib import Path
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ProductionParser] %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class DocumentParser:
    """
    Advanced Document Parser for SubmissionVeritas Organization.
    Handles PDF, DOCX, XLSX, and CSV with persistent hashing.
    """
    
    @staticmethod
    def get_content_hash(text: str) -> str:
        """Generates a unique SHA-256 hash for the document text."""
        if not text:
            return "empty_content"
        # We hash the text to see if the CONTENT has changed (delta detection)
        return hashlib.sha256(text.encode('utf-8', errors='ignore')).hexdigest()

    @staticmethod
    def parse_pdf(file_path: str) -> str:
        text = ""
        is_scanned = False
        try:
            with fitz.open(file_path) as doc:
                for page in doc:
                    page_text = page.get_text()
                    text += page_text + "\n"
                    # CRITICAL: If any page has images OR text is 0, mark as potential scan
                    if len(page.get_images()) > 0 or len(page_text.strip()) == 0:
                        is_scanned = True
        except Exception as e:
            logger.error(f"Error parsing PDF {file_path}: {e}")
            
        text = text.strip()
        
        # DEBUG: Log results before OCR
        logger.info(f"  [PARSER] Initial text length: {len(text)}. is_scanned: {is_scanned}")

        # OCR fallback: Force trigger if text is very short
        if len(text) < 200:
            logger.info(f"  [OCR] Low text detected ({len(text)} chars). Forcing OCR for {Path(file_path).name}...")
            try:
                import pytesseract
                from pdf2image import convert_from_path
                from PIL import Image
                
                # Limit to 10 pages maximum
                images = convert_from_path(file_path, first_page=1, last_page=10)
                ocr_text = ""
                for i, img in enumerate(images):
                    ocr_text += pytesseract.image_to_string(img) + "\n"
                    
                text = ocr_text.strip()
                logger.info(f"  [OCR] Successfully extracted {len(text)} characters.")
            except Exception as e:
                logger.error(f"  [OCR] Engine failed: {e}")
                
        return text

    @staticmethod
    def parse_docx(file_path: str) -> str:
        text = ""
        try:
            doc = docx.Document(file_path)
            for para in doc.paragraphs:
                text += para.text + "\n"
        except Exception as e:
            logger.error(f"Error parsing DOCX {file_path}: {e}")
        return text.strip()

    @staticmethod
    def parse_excel(file_path: str) -> str:
        text = ""
        try:
            df = pd.read_excel(file_path, sheet_name=None)
            for sheet_name, sheet_data in df.items():
                text += f"--- Sheet: {sheet_name} ---\n"
                text += sheet_data.to_string(index=False) + "\n"
        except Exception as e:
            logger.error(f"Error parsing Excel {file_path}: {e}")
        return text.strip()

    @classmethod
    def process_file(cls, file_path: str):
        """
        Main entry point. Returns (text_content, hash_string).
        """
        path = Path(file_path)
        if not path.exists():
            return "", "error_missing_file"
        
        ext = path.suffix.lower()
        content = ""
        
        if ext == '.pdf':
            content = cls.parse_pdf(str(path))
        elif ext in ['.docx', '.doc']:
            content = cls.parse_docx(str(path))
        elif ext in ['.xlsx', '.xls', '.csv']:
            if ext == '.csv':
                try:
                   content = pd.read_csv(str(path)).to_string(index=False)
                except:
                   pass
            else:
                content = cls.parse_excel(str(path))
        else:
            logger.warning(f"Unsupported format: {ext}")
            content = f"[UNSUPPORTED FORMAT: {ext}]"

        content_hash = cls.get_content_hash(content)
        return content, content_hash

if __name__ == "__main__":
    print("SubmissionVeritas Production Parser Initialized")
