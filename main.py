# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
# from typing import Dict

# 1. Initialize the FastAPI application
app = FastAPI()

# 2. Define the structure of the data we expect (Pydantic Model)
class WordInput(BaseModel):
    """Defines the structure for the data we expect from the user."""
    word_input: str

# --- API Endpoints ---

@app.post("/submit/")
def submit_word(data: WordInput):
    """
    API endpoint to receive a word and process it.
    This simulates receiving data from a client (like a web form).
    """
    # The data is automatically validated by Pydantic before this line runs.
    word = data.word_input
    
    print(f"Server received the word: {word}")

    # Simulate successful processing
    return {
        "status": "success",
        "message": f"The word '{word}' was successfully received and processed!"
    }

# Optional: A basic root endpoint to check if the server is running
@app.get("/")
def read_root():
    return {"message": "Welcome to the Word Input API. Use /submit/ to send a word."}

@app.get("/health")
def health():
    return {"status": "healthy"}
