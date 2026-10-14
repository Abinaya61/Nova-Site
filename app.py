import os
import json
import time
import pdfplumber
import docx
from google import genai
from google.genai import types
from flask import Flask, request, jsonify, render_template, Response, stream_with_context, send_from_directory
from werkzeug.utils import secure_filename
import urllib.parse
import urllib.request

app = Flask(__name__)

UPLOAD_FOLDER = os.path.abspath('uploads')
GENERATED_FOLDER = os.path.abspath('generated_images')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(GENERATED_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['GENERATED_FOLDER'] = GENERATED_FOLDER

# 1. API Key Setup
def load_api_key():
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        return env_key
    json_path = 'main.json'
    if os.path.exists(json_path):
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get("GEMINI_API_KEY") or data.get("api_key")
    return None

API_KEY = load_api_key()
if not API_KEY:
    raise ValueError("GEMINI_API_KEY கிடைக்கவில்லை!")

client = genai.Client(api_key=API_KEY)

# பதிவேற்றப்பட்ட ஆவணங்களின் உரையைச் சேமிக்கும் பட்டியல்
DOCUMENT_PAGES = []
ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.txt', '.png', '.jpg', '.jpeg', '.webp'}

def extract_pages(file_path, filename):
    ext = os.path.splitext(filename)[1].lower()
    pages_data = []
    try:
        if ext == '.pdf':
            with pdfplumber.open(file_path) as pdf:
                for idx, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        pages_data.append({
                            'source': filename,
                            'page': str(idx + 1),
                            'text': text.strip()
                        })
        elif ext in ['.docx', '.doc']:
            doc = docx.Document(file_path)
            full_text = "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])
            if full_text:
                pages_data.append({
                    'source': filename,
                    'page': 'Full Document',
                    'text': full_text
                })
        elif ext == '.txt':
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read().strip()
                if content:
                    pages_data.append({
                        'source': filename,
                        'page': 'Full Document',
                        'text': content
                    })
    except Exception as e:
        print(f"Error extracting {filename}: {e}")
    return pages_data

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/files/<path:filename>')
def serve_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/generated/<path:filename>')
def serve_generated_file(filename):
    return send_from_directory(app.config['GENERATED_FOLDER'], filename)

@app.route('/delete-chat-assets', methods=['POST'])
def delete_chat_assets():
    global DOCUMENT_PAGES
    data = request.json or {}
    filenames = data.get('filenames', [])
    
    deleted_files = []
    for name in filenames:
        safe_name = os.path.basename(name)
        
        # 1. generated_images folder-la irunthu neekuthal
        gen_path = os.path.join(app.config['GENERATED_FOLDER'], safe_name)
        if os.path.exists(gen_path):
            try:
                os.remove(gen_path)
                deleted_files.append(safe_name)
            except Exception:
                pass

        # 2. uploads folder-la irunthu neekuthal (images matrum docs)
        up_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
        if os.path.exists(up_path):
            try:
                os.remove(up_path)
                deleted_files.append(safe_name)
            except Exception:
                pass

        # 3. Memory-la irukkura DOCUMENT_PAGES-la irunthum antha doc-ai neekuthal
        DOCUMENT_PAGES = [doc for doc in DOCUMENT_PAGES if doc['source'] != safe_name]

    return jsonify({'success': True, 'deleted': deleted_files})

@app.route('/upload', methods=['POST'])
def upload_docs():
    global DOCUMENT_PAGES
    if 'files' not in request.files:
        return jsonify({'error': 'No files provided'}), 400
        
    files = request.files.getlist('files')
    uploaded_files = []

    for file in files:
        if file.filename:
            safe_name = secure_filename(file.filename)
            ext = os.path.splitext(safe_name)[1].lower()
            if ext in ALLOWED_EXTENSIONS:
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
                file.save(file_path)
                
                extracted = extract_pages(file_path, safe_name)
                # பழைய ஆவணத்தின் அதே பெயர் இருந்தால் நீக்கிவிட்டு புதியதைச் சேர்த்தல்
                DOCUMENT_PAGES = [doc for doc in DOCUMENT_PAGES if doc['source'] != safe_name]
                DOCUMENT_PAGES.extend(extracted)
                
                uploaded_files.append(safe_name)

    return jsonify({'message': 'Success', 'files': uploaded_files})

@app.route('/delete-file', methods=['POST'])
def delete_file():
    global DOCUMENT_PAGES
    data = request.json
    filename = data.get('filename')
    if not filename:
        return jsonify({'error': 'Filename required'}), 400

    try:
        DOCUMENT_PAGES = [item for item in DOCUMENT_PAGES if item['source'] != filename]
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(filename))
        if os.path.exists(file_path):
            os.remove(file_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Chat & Document / Image Analysis with Page Citation
@app.route('/chat', methods=['POST'])
def chat():
    query = request.form.get('query', '').strip()
    language = request.form.get('language', 'ta-IN')
    image_file = request.files.get('image')

    lang_text = "Tamil (தமிழ்)" if language == 'ta-IN' else "English"

    # Document context with page numbers
    context_snippets = [f"[File: {item['source']} | Page: {item['page']}]\n{item['text']}" for item in DOCUMENT_PAGES]
    final_context = "\n\n---\n\n".join(context_snippets).strip()

    # Simple & strict prompt for citations
    if final_context:
        system_instruction = f"""You are NovaSight document assistant.
Answer accurately based on the provided documents or uploaded images.
Strict Rule: At the end of every answer or factual claim, cite the source clearly:
Source: [Filename, Page X]
Language: {lang_text}.

Context:
{final_context}"""
    else:
        system_instruction = f"""You are NovaSight assistant.
Answer the user query or analyze the uploaded image clearly.
Language: {lang_text}."""

    contents = []

    # Uploaded image handling
    if image_file and image_file.filename:
        filename = f"{int(time.time())}_{secure_filename(image_file.filename)}"
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        image_file.save(file_path)
        
        with open(file_path, "rb") as f:
            img_bytes = f.read()
            contents.append(types.Part.from_bytes(data=img_bytes, mime_type=image_file.mimetype or "image/jpeg"))

    contents.append(query if query else "Explain this.")

    def generate():
        # Athiga load thalaikattal irukkumpothu maatri try panna virivana model list
        models_to_try = [
            'gemini-3.5-flash-lite',
            'gemini-3.1-flash-lite',
            'gemini-3.8-flash'
        ]
        last_error = None

        for model_name in models_to_try:
            try:
                chat_session = client.chats.create(
                    model=model_name,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction
                    )
                )
                response = chat_session.send_message_stream(contents)
                for chunk in response:
                    if chunk.text:
                        yield chunk.text
                return  # Vetrigaramaaga response vanthuvittaal mudithuvidavum
            except Exception as e:
                last_error = str(e)
                # 503, unavailable allathu overloaded vanthaal adutha model-ku pogavum
                if any(err_term in last_error.lower() for err_term in ["503", "unavailable", "overloaded", "high demand"]):
                    time.sleep(1)  # Server spike-kaga 1 nodi thaamatham
                    continue
                elif "404" in last_error:
                    # Model peyar kidaikkavillai endraalum adutha model-ku maara vendum
                    continue
                else:
                    yield f"\n[Error ({model_name}): {last_error}]"
                    return

        # Anaithu models-um overload aanaal mattum
        yield f"\n[Error: Server load athigamaaga ullathu. Sirithu neram kazhithu try seiyavum. (Kadaisi: {last_error})]"

    return Response(stream_with_context(generate()), mimetype='text/plain')


@app.route('/generate-image', methods=['POST'])
def generate_image_route():
    data = request.json or {}
    prompt = data.get('prompt', '').strip()

    if not prompt:
        return jsonify({'error': 'விளக்கம் தேவை'}), 400

    try:
        encoded_prompt = urllib.parse.quote(prompt)
        # model=flux மற்றும் nologo அளவுருவைச் சேர்க்கவும்
        image_api_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=768&height=768&model=flux&nologo=true"

        filename = f"gen_{int(time.time())}.png"
        file_path = os.path.join(app.config['GENERATED_FOLDER'], filename)

        req = urllib.request.Request(
            image_api_url, 
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req) as resp, open(file_path, 'wb') as f:
            f.write(resp.read())

        return jsonify({'image_url': f"/generated/{filename}", 'filename': filename})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)