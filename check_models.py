import google.generativeai as genai
import toml

# Leggiamo la chiave direttamente dal file secrets di Streamlit
secrets = toml.load(".streamlit/secrets.toml")
genai.configure(api_key=secrets["GEMINI_API_KEY"])

print("Modelli supportati per l'analisi dei documenti:")
for m in genai.list_models():
    if 'generateContent' in m.supported_generation_methods:
        print(f"Nome da copiare: {m.name}")