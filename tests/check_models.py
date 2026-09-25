"""Optional live diagnostic. Run explicitly; never executed by the offline tests."""
import tomllib
from pathlib import Path
from google import genai

# Leggiamo la chiave direttamente dal file secrets di Streamlit
if __name__ == "__main__":
    path = Path(__file__).resolve().parents[1] / ".streamlit" / "secrets.toml"
    with path.open("rb") as stream:
        secrets = tomllib.load(stream)
    with genai.Client(api_key=secrets["GEMINI_API_KEY"]) as client:
        print("Modelli disponibili:")
        for model in client.models.list():
            print(model.name)
