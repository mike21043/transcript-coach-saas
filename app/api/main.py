from fastapi import FastAPI
app = FastAPI(title="TranscriptCoach API - production scaffold")
@app.get('/health')
def health():
    return {'status':'ok'}
