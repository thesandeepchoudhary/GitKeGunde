from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import urllib.parse
import json
import httpx
import os

app = FastAPI()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "YOUR_GITHUB_ACCESS_TOKEN")
print(os.getenv("OPENAI_API_KEY"))

async def generate_review_comment(diff: str) -> str:
    url = "http://localhost:11434/api/generate"
    prompt = (
        "You are a senior code reviewer. Review the following GitHub pull request diff and provide helpful, concise feedback.\n\n"
        f"{diff[:4000]}"
    )
    data = {
        "model": "llama3.2",
        "prompt": prompt,
        "stream": False
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=data)
        resp.raise_for_status()
        result = resp.json()
        return result["response"].strip()


async def get_pr_diff(repo_full_name: str, pr_number: int, token: str) -> str:
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.diff",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text

async def find_pr_number(repo_full_name: str, branch: str, token: str) -> int | None:
    url = f"https://api.github.com/repos/{repo_full_name}/pulls"
    headers = {"Authorization": f"token {token}"}
    params = {"state": "open"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        prs = resp.json()

    for pr in prs:
        if pr["head"]["ref"] == branch:
            return pr["number"]
    return None

async def post_pr_comment(repo_full_name: str, pr_number: int, token: str, comment_text: str):
    comment_url = f"https://api.github.com/repos/{repo_full_name}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    comment_body = {"body": comment_text}
    async with httpx.AsyncClient() as client:
        resp = await client.post(comment_url, headers=headers, json=comment_body)
        resp.raise_for_status()
        return resp.json()

@app.post("/")
async def webhook_handler(request: Request):
    # Try parsing JSON payload directly
    try:
        payload_dict = await request.json()
    except Exception:
        # If not JSON, try parse urlencoded payload param
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8")
        if body_str.startswith("payload="):
            encoded_json_str = body_str.split("=", 1)[1]
            decoded_json_str = urllib.parse.unquote(encoded_json_str)
            payload_dict = json.loads(decoded_json_str)
        else:
            raise HTTPException(status_code=400, detail="Invalid payload format")

    repo_full_name = payload_dict.get("repository", {}).get("full_name")
    ref = payload_dict.get("ref")
    if not repo_full_name or not ref:
        raise HTTPException(status_code=400, detail="Missing repository info or ref")

    branch = ref.split("/")[-1]

    try:
        pr_number = await find_pr_number(repo_full_name, branch, GITHUB_TOKEN)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=500, detail=f"GitHub API error: {e}")

    if not pr_number:
        return JSONResponse(content={"message": f"No open PR found for branch {branch}"}, status_code=200)

     # Fetch PR diff
    diff = await get_pr_diff(repo_full_name, pr_number, GITHUB_TOKEN)
    #comment_text = "Hello! This is an automated comment from the webhook."

       # Generate comment using OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY")
    comment_text = await generate_review_comment(diff)

    try:
        comment_resp = await post_pr_comment(repo_full_name, pr_number, GITHUB_TOKEN, comment_text)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=500, detail=f"Failed to post comment: {e}")

    return {
        "message": f"Comment posted on PR #{pr_number}",
        "comment": comment_resp,
    }

@app.get("/")
async def root():
    return {"message": "GitKeGunde server is running."}