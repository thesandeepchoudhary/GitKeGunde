from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import urllib.parse
import json
import httpx
import os
import openai
import re
import pprint
import subprocess
import tempfile
import shutil
import base64
from datetime import datetime

# Initialize GPT client (LiteLLM proxy)
gptClient = openai.OpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "YOUR_OPENAI_API_KEY"),
    base_url="https://litellm-data.penpencil.co"
)

app = FastAPI()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "YOUR_GITHUB_ACCESS_TOKEN")

def extract_changed_lines(diff_text):
    """
    Parses unified diff and returns a list of (removed_line, added_line) tuples.
    """
    lines = diff_text.splitlines()
    changes = []
    prev_line = None
    for line in lines:
        if line.startswith("-") and not line.startswith("---"):
            prev_line = line[1:].strip()
        elif line.startswith("+") and not line.startswith("+++"):
            if prev_line:
                changes.append((prev_line, line[1:].strip()))
                prev_line = None
            else:
                changes.append(("", line[1:].strip()))
    return changes

def validate_diff_suggestions(changes):
    known_invalid_map = {
        "display: blocks;": "display: block;",
    }
    suggestions = []
    for removed, added in changes:
        correction = known_invalid_map.get(removed)
        if correction and correction != added:
            suggestions.append({
                "removed": removed,
                "added": correction
            })
    return suggestions


def map_line_to_position(patch: str):
    line_map = {}
    position = 0
    new_line_num = 0
    for line in patch.splitlines():
        position += 1
        if line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                new_line_num = int(m.group(1)) - 1
        elif line.startswith("+") or line.startswith(" "):
            new_line_num += 1
            line_map[new_line_num] = position
        elif line.startswith("-"):
            continue
    return line_map

async def post_inline_comment(repo_full_name, pr_number, token, commit_id, file_path, position, body):
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}/comments"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "body": body,
        "commit_id": commit_id,
        "path": file_path,
        "position": position,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()

async def find_pr_number(repo_full_name: str, branch: str, token: str):
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
        "You are a senior software engineer and service architect reviewing a pull request.\n"
        f"Pull Request Title: {pr_title}\n"
        f"Description: {pr_description}\n"
        "\nYour review must be comprehensive and address the following aspects for each file and diff:\n"
        "1. **Bugs and Issues**: Identify any potential bugs, logic errors, or problematic code.\n"
        "2. **Security Concerns**: Point out security vulnerabilities or best practices violations.\n"
        "3. **Performance**: Note any performance implications and optimization opportunities.\n"
        "4. **Code Quality**: Comment on code style, maintainability, and adherence to best practices.\n"
        "5. **Suggestions**: Provide constructive improvements and recommendations.\n"
        "\nFor each issue found, use this exact structured format:\n"
        "File: <file path>\n"
        "Line: <line number>\n"
        "Severity: <Critical | Major | Minor | Info>\n"
        "Issue: <short description>\n"
        "Suggestion: <explanation>\n"
        "```suggestion\n<full corrected line(s) to copy-paste>\n```\n"
        "At the end, include a sequence diagram\n"
    )
    for file_review in file_reviews:
        prompt += (
            f"\n---\nFile: {file_review['file']}\nFull Content After Changes:\n{file_review['full']}\nDiff:\n{file_review['diff']}"
        )
    return prompt

async def generate_review_comment(file_reviews, pr_title, pr_description) -> str:
    prompt = build_openai_prompt(pr_title, pr_description, file_reviews)
    response = gptClient.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": "You are an expert software engineer and service architect performing a detailed code review."},
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


def apply_suggestions_locally(repo_url: str, base_branch: str, file_suggestions: dict):
    tmp_dir = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "clone", "--branch", base_branch, f"https://x-access-token:{GITHUB_TOKEN}@github.com/{repo_url}.git", tmp_dir], check=True)
        subprocess.run(["git", "checkout", "-b", "auto-fix-" + datetime.now().strftime("%Y%m%d%H%M%S")], cwd=tmp_dir, check=True)

        for file_path, changes in file_suggestions.items():
            full_path = os.path.join(tmp_dir, file_path)
            if not os.path.exists(full_path):
                continue
            with open(full_path, "r") as f:
                lines = f.readlines()
            for line_num, suggestion in changes.items():
                if 0 < line_num <= len(lines):
                    lines[line_num - 1] = suggestion + "\n"
            with open(full_path, "w") as f:
                f.writelines(lines)

        subprocess.run(["git", "config", "user.name", "AutoBot"], cwd=tmp_dir)
        subprocess.run(["git", "config", "user.email", "autobot@example.com"], cwd=tmp_dir)
        subprocess.run(["git", "add", "."], cwd=tmp_dir)
        subprocess.run(["git", "commit", "-m", "chore: apply suggested changes from review"], cwd=tmp_dir)
        subprocess.run(["git", "push", "--set-upstream", "origin", "HEAD"], cwd=tmp_dir)
        result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=tmp_dir, stdout=subprocess.PIPE)
        new_branch = result.stdout.decode().strip()
        return new_branch
    finally:
        shutil.rmtree(tmp_dir)


async def create_new_pr(repo: str, new_branch: str, base_branch: str, token: str):
    url = f"https://api.github.com/repos/{repo}/pulls"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    payload = {
        "title": "Automated Fix: Suggested Code Improvements",
        "head": new_branch,
        "base": base_branch,
        "body": "This PR contains automated fixes based on GPT code review suggestions."
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()


@app.post("/")
async def webhook_handler(request: Request):
    try:
        payload_dict = await request.json()
    except Exception:
        body_bytes = await request.body()
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
    base_branch = ""

    if "pull_request" in payload_dict:
        pr = payload_dict["pull_request"]
        pr_number = pr.get("number")
        head_sha = pr.get("head", {}).get("sha")
        base_branch = pr.get("base", {}).get("ref", "")
        pr_title = pr.get("title", "")
        pr_description = pr.get("body", "")
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
        base_branch = pr_metadata.get("base", {}).get("ref")
        pr_title = pr_metadata.get("title", "")
        pr_description = pr_metadata.get("body", "")
        print("Reached push event")
    else:
        return JSONResponse(content={"message": "Event ignored: not a PR or push event."}, status_code=200)

    if branch.startswith("auto-fix-"):
        return JSONResponse(content={"message": "Event ignored: This is auto-fix PR"}, status_code=200)

    if not repo_full_name or not pr_number:
        raise HTTPException(status_code=400, detail="Missing repository info or PR number")

    pr_files = await get_pr_files(repo_full_name, pr_number, GITHUB_TOKEN)
    file_reviews = []
    file_patch_maps = {}

    for file in pr_files:
        file_path = file.get("filename")
        patch = file.get("patch", "") or ""
        full_content = await get_file_content(repo_full_name, file_path, head_sha, GITHUB_TOKEN)
        file_reviews.append({"file": file_path, "diff": patch, "full": full_content})
        file_patch_maps[file_path] = map_line_to_position(patch)

        changes = extract_changed_lines(patch)
        corrections = validate_diff_suggestions(changes)
        for fix in corrections:
            print(f"Auto-fix: \n- {fix['removed']}\n+ {fix['added']}")

    comment_text = await generate_review_comment(file_reviews, pr_title, pr_description)
    comment_resp = await post_pr_comment(repo_full_name, pr_number, GITHUB_TOKEN, comment_text)

    blocks = comment_text.split("File:")
    file_suggestions = {}
    for block in blocks[1:]:
        lines = block.strip().splitlines()
        current_file = lines[0].strip()
        line_num = None
        severity = None
        issue = None
        suggestion = None
        in_code_block = False
        suggestion_code = ""

        for line in lines[1:]:
            if line.startswith("Line:"):
                try:
                    line_num = int(line.split(":", 1)[1].strip())
                except Exception:
                    line_num = None
            elif line.startswith("Severity:"):
                severity = line.split(":", 1)[1].strip()
            elif line.startswith("Issue:"):
                issue = line.split(":", 1)[1].strip()
            elif line.startswith("Suggestion:"):
                suggestion = line.split(":", 1)[1].strip()
            elif line.strip().startswith("```suggestion"):
                in_code_block = True
                suggestion_code = ""
            elif line.strip() == "```" and in_code_block:
                in_code_block = False
            elif in_code_block:
                suggestion_code += line + "\n"

        if current_file in file_patch_maps and line_num in file_patch_maps[current_file]:
            position = file_patch_maps[current_file][line_num]
            comment_body = (
                f"**{severity or 'Info'}**\n\nIssue: {issue}\n\nSuggestion: {suggestion}\n\n```suggestion\n{suggestion_code.strip()}\n```"
            )
            try:
                await post_inline_comment(
                    repo_full_name, pr_number, GITHUB_TOKEN,
                    head_sha, current_file, position, comment_body
                )
            except Exception as e:
                print(f"Failed to post inline comment on {current_file} line {line_num}: {e}")
        else:
            print(f"Could not map file/line to diff position: {current_file} line {line_num}")
        if current_file and line_num and suggestion_code:
            file_suggestions.setdefault(current_file, {})[line_num] = suggestion_code

    new_branch = apply_suggestions_locally(repo_full_name, branch, file_suggestions)
    pr = await create_new_pr(repo_full_name, new_branch, branch, GITHUB_TOKEN)

    return {"message": f"Comment posted on PR #{pr_number}", "comment": comment_resp}
