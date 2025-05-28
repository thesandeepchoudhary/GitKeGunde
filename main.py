# File: main.py
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import urllib.parse
import json
import httpx
import os
import openai 

gptClient = openai.OpenAI(
    api_key='sk-8Cw-NcHRqJ0YOR614yUliQ',
    base_url="https://litellm-data.penpencil.co"  # Your LiteLLM proxy
)

app = FastAPI()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "YOUR_GITHUB_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY")

async def find_pr_number(repo_full_name: str, branch: str, token: str):
    """
    Find the open PR number for a given branch in a repository.
    Returns the PR number if found, else None.
    """
    url = f"https://api.github.com/repos/{repo_full_name}/pulls?state=open&head={repo_full_name.split('/')[0]}:{branch}"
    headers = {"Authorization": f"token {token}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        prs = resp.json()
        if prs:
            return prs[0]["number"]
        return None

def build_openai_prompt(pr_title, pr_description, file_reviews):
    prompt = (
        f"You are a senior software engineer and code reviewer."
        f"\nPull Request Title: {pr_title}"
        f"\nDescription: {pr_description}\n"
        "\nReview the following file changes. Focus on quality, security, maintainability, and best practices."
        "\nGive specific feedback per file and line if possible.\n"
    )
    for file_review in file_reviews:
        prompt += f"\n\n---\nFile: {file_review['file']}\nFull Content After Changes:\n{file_review['full']}\nDiff:\n{file_review['diff']}"
    prompt += "\n\nProvide a consolidated, structured review."
    return prompt

async def generate_review_comment(file_reviews, pr_title, pr_description) -> str:

    prompt = build_openai_prompt(pr_title, pr_description, file_reviews)

    response = gptClient.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": "You are an expert software engineer and code reviewer."},
            {"role": "user", "content": prompt}
        ]
    )
    return response.choices[0].message.content.strip()

async def get_pr_metadata(repo_full_name: str, pr_number: int, token: str):
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {"Authorization": f"token {token}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

async def get_pr_files(repo_full_name: str, pr_number: int, token: str):
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/files"
    headers = {"Authorization": f"token {token}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

async def get_file_content(repo_full_name: str, file_path: str, ref: str, token: str):
    url = f"https://api.github.com/repos/{repo_full_name}/contents/{file_path}?ref={ref}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3.raw"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.text
        return ""

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
    try:
        payload_dict = await request.json()
    except Exception:
        body_bytes = await request.body()
        print("Raw incoming body:", body_bytes.decode("utf-8"))
        body_str = body_bytes.decode("utf-8")
        if body_str.startswith("payload="):
            encoded_json_str = body_str.split("=", 1)[1]
            decoded_json_str = urllib.parse.unquote(encoded_json_str)
            payload_dict = json.loads(decoded_json_str)
        else:
            raise HTTPException(status_code=400, detail="Invalid payload format")

    repo_full_name = payload_dict.get("repository", {}).get("full_name")
    pr_number = None
    head_sha = None
    pr_title = ""
    pr_description = ""

    # Handle PR event
    if "pull_request" in payload_dict:
        pr = payload_dict["pull_request"]
        pr_number = pr.get("number")
        head_sha = pr.get("head", {}).get("sha")
        pr_title = pr.get("title", "")
        pr_description = pr.get("body", "")
    # Handle push event (find PR by branch)
    elif "ref" in payload_dict:
        ref = payload_dict.get("ref")
        branch = ref.split("/")[-1]
        if not repo_full_name or not branch:
            raise HTTPException(status_code=400, detail="Missing repository info or branch")
        pr_number = await find_pr_number(repo_full_name, branch, GITHUB_TOKEN)
        if not pr_number:
            return JSONResponse(content={"message": f"No open PR found for branch {branch}"}, status_code=200)
        pr_metadata = await get_pr_metadata(repo_full_name, pr_number, GITHUB_TOKEN)
        head_sha = pr_metadata.get("head", {}).get("sha")
        pr_title = pr_metadata.get("title", "")
        pr_description = pr_metadata.get("body", "")
    else:
        return JSONResponse(content={"message": "Event ignored: not a PR or push event."}, status_code=200)

    if not repo_full_name or not pr_number:
        raise HTTPException(status_code=400, detail="Missing repository info or PR number")

    pr_files = await get_pr_files(repo_full_name, pr_number, GITHUB_TOKEN)

    file_reviews = []
    for file in pr_files:
        file_path = file.get("filename")
        patch = file.get("patch", "")
        full_content = await get_file_content(repo_full_name, file_path, head_sha, GITHUB_TOKEN)
        file_reviews.append({"file": file_path, "diff": patch, "full": full_content})

    comment_text = await generate_review_comment(
        file_reviews,
        pr_title=pr_title,
        pr_description=pr_description
    )

    comment_resp = await post_pr_comment(repo_full_name, pr_number, GITHUB_TOKEN, comment_text)
    return {"message": f"Comment posted on PR #{pr_number}", "comment": comment_resp}

@app.get("/")
async def root():
    return {"message": "Enhanced GitHub PR reviewer is running."}