from flask import Flask, request, jsonify
import os
import tempfile
import subprocess
import json
import httpx
import asyncio
import threading
import shutil
import hmac
import hashlib
import urllib.parse
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# Load env variables
LITELLM_URL = os.getenv("LITELLM_URL")
LITELLM_KEY = os.getenv("LITELLM_KEY")
MODEL_ID = os.getenv("MODEL_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # Add webhook secret for security

def verify_webhook_signature(payload_body, signature_header):
    """Skip webhook signature verification"""
    return True  # Always allow - no signature verification

# Clone repo with both PR branch and base branch
def clone_repo(repo_url, pr_branch, base_branch="main"):
    temp_dir = tempfile.mkdtemp()
    try:
        # Use token authentication for private repos
        if GITHUB_TOKEN:
            auth_url = repo_url.replace("https://", f"https://{GITHUB_TOKEN}@")
        else:
            auth_url = repo_url
        
        print(f"🔄 Cloning repository to {temp_dir}")
        # Clone the entire repository (not shallow) to get all branches
        subprocess.run(
            ["git", "clone", auth_url, temp_dir], 
            check=True,
            capture_output=True,
            text=True
        )
        
        # Checkout the PR branch
        print(f"🔄 Checking out PR branch: {pr_branch}")
        subprocess.run(
            ["git", "checkout", pr_branch], 
            cwd=temp_dir,
            check=True,
            capture_output=True,
            text=True
        )
        
        return temp_dir
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to clone repo: {e.stderr}")
        raise

# Collect all relevant source files
def get_repo_context(repo_path):
    allowed_extensions = (".py", ".js", ".ts", ".tsx", ".scss", ".css", ".java", ".cpp", ".c", ".h")
    all_files = []
    max_file_size = 50000  # Limit file size to prevent token overflow

    for root, _, files in os.walk(repo_path):
        # Skip common directories that aren't relevant for review
        if any(skip_dir in root for skip_dir in ['.git', 'node_modules', '__pycache__', '.pytest_cache', 'venv', 'env']):
            continue
            
        for file in files:
            if file.endswith(allowed_extensions):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        if len(content) > max_file_size:
                            content = content[:max_file_size] + "\n... (file truncated)"
                        relative_path = os.path.relpath(file_path, repo_path)
                        all_files.append(f"File: {relative_path}\n{content}")
                except Exception as e:
                    print(f"⚠️ Failed to read {file_path}: {e}")

    return "\n\n".join(all_files)

# Get diff between PR branch and base branch
def get_diff(repo_path, base_branch="main"):
    try:
        print(f"🔍 Getting diff between current branch and {base_branch}")
        
        # First, let's see what branches are available
        result = subprocess.run(
            ["git", "branch", "-r"], 
            cwd=repo_path, 
            capture_output=True, 
            text=True
        )
        print(f"📋 Available remote branches: {result.stdout.strip()}")
        
        # Try to get diff against origin/base_branch first
        result = subprocess.run(
            ["git", "diff", f"origin/{base_branch}"], 
            cwd=repo_path, 
            capture_output=True, 
            text=True
        )
        
        if result.returncode == 0:
            print(f"✅ Successfully got diff against origin/{base_branch}")
            return result.stdout
        
        print(f"⚠️ Failed to diff against origin/{base_branch}: {result.stderr}")
        
        # If that fails, try with master
        if base_branch != "master":
            print("🔍 Trying with master branch")
            result = subprocess.run(
                ["git", "diff", "origin/master"], 
                cwd=repo_path, 
                capture_output=True, 
                text=True
            )
            
            if result.returncode == 0:
                print("✅ Successfully got diff against origin/master")
                return result.stdout
            
            print(f"⚠️ Failed to diff against origin/master: {result.stderr}")
        
        # If both fail, try to get the merge base and diff against that
        print("🔍 Trying to find merge base")
        merge_base_result = subprocess.run(
            ["git", "merge-base", "HEAD", f"origin/{base_branch}"], 
            cwd=repo_path, 
            capture_output=True, 
            text=True
        )
        
        if merge_base_result.returncode == 0:
            merge_base = merge_base_result.stdout.strip()
            print(f"🔍 Found merge base: {merge_base}")
            
            result = subprocess.run(
                ["git", "diff", merge_base], 
                cwd=repo_path, 
                capture_output=True, 
                text=True
            )
            
            if result.returncode == 0:
                print("✅ Successfully got diff against merge base")
                return result.stdout
        
        print("❌ All diff attempts failed")
        return ""
        
    except Exception as e:
        print(f"❌ Exception in get_diff: {e}")
        return ""

# Improved health check function for LiteLLM
async def check_litellm_health():
    """Check if LiteLLM service is accessible by making a simple API call"""
    try:
        # Increased timeout configuration for slower responses
        timeout_config = httpx.Timeout(
            connect=10.0,  # 10 seconds to establish connection
            read=60.0,     # 60 seconds to read response
            write=10.0,    # 10 seconds to write request
            pool=10.0      # 10 seconds for connection pool
        )
        
        # Add more headers to mimic a real browser/client
        headers = {
            "Authorization": f"Bearer {LITELLM_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "AI-Code-Reviewer/1.0",
            "Accept": "application/json"
        }
        
        # Use the exact same payload that works in your manual test
        payload = {
            "model": MODEL_ID,
            "messages": [
                {
                    "role": "user",
                    "content": "this is a test request, write a short poem"
                }
            ]
        }
        
        print(f"🔍 Testing LiteLLM API at: {LITELLM_URL}")
        print(f"🤖 Using model: {MODEL_ID}")
        print(f"📝 Request headers: {headers}")
        print(f"📋 Request payload: {payload}")
        
        async with httpx.AsyncClient(timeout=timeout_config, verify=False) as client:
            print("🔄 Sending request to LiteLLM...")
            start_time = asyncio.get_event_loop().time()
            
            response = await client.post(LITELLM_URL, headers=headers, json=payload)
            
            end_time = asyncio.get_event_loop().time()
            duration = end_time - start_time
            
            print(f"📊 Response status: {response.status_code}")
            print(f"⏱️ Request duration: {duration:.2f} seconds")
            print(f"📏 Response size: {len(response.content)} bytes")
            
            if response.status_code == 200:
                result = response.json()
                print("✅ LiteLLM health check successful")
                print(f"📄 Response preview: {str(result)[:200]}...")
                return True
            else:
                print(f"❌ LiteLLM health check failed with status: {response.status_code}")
                print(f"📄 Response headers: {dict(response.headers)}")
                print(f"📄 Response body: {response.text}")
                return False
                
    except httpx.ConnectTimeout as e:
        print(f"❌ LiteLLM health check - connection timeout after 10s: {e}")
        print("💡 This suggests the server is not reachable or very slow to respond")
        return False
    except httpx.ReadTimeout as e:
        print(f"❌ LiteLLM health check - read timeout after 60s: {e}")
        print("💡 Server connected but response took too long")
        return False
    except httpx.HTTPStatusError as e:
        print(f"❌ LiteLLM health check - HTTP error: {e.response.status_code}")
        print(f"📄 Response: {e.response.text}")
        return False
    except Exception as e:
        print(f"❌ LiteLLM health check failed with exception: {e}")
        print(f"🔍 Exception type: {type(e).__name__}")
        import traceback
        traceback.print_exc()
        return False

# Improved async call to LiteLLM proxy with better error handling
async def call_litellm(prompt):
    # Add more headers to match working configuration
    headers = {
        "Authorization": f"Bearer {LITELLM_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "AI-Code-Reviewer/1.0",
        "Accept": "application/json"
    }
    
    # Simplified payload without max_tokens and temperature
    payload = {
        "model": MODEL_ID,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ]
    }

    # Increased timeout and added retries
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            # Use more granular timeout configuration
            timeout_config = httpx.Timeout(
                connect=10.0,  # 10 seconds to connect
                read=180.0,    # 3 minutes to read (for long responses)
                write=10.0,    # 10 seconds to write
                pool=10.0      # 10 seconds for pool
            )
            
            async with httpx.AsyncClient(timeout=timeout_config, verify=False) as client:
                print(f"🔄 Attempting LiteLLM API call (attempt {attempt + 1}/{max_retries})")
                print(f"📡 URL: {LITELLM_URL}")
                
                start_time = asyncio.get_event_loop().time()
                response = await client.post(LITELLM_URL, headers=headers, json=payload)
                end_time = asyncio.get_event_loop().time()
                
                print(f"⏱️ Request took {end_time - start_time:.2f} seconds")
                response.raise_for_status()
                
                result = response.json()
                print("✅ LiteLLM API call successful")
                return result["choices"][0]["message"]["content"]
                
        except httpx.ConnectTimeout:
            print(f"❌ Connection timeout on attempt {attempt + 1}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5  # Exponential backoff
                print(f"⏳ Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)
            else:
                print("❌ All connection attempts failed - LiteLLM service may be down")
                raise Exception("LiteLLM service unreachable after multiple attempts")
                
        except httpx.ReadTimeout:
            print(f"❌ Read timeout on attempt {attempt + 1}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5
                print(f"⏳ Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)
            else:
                raise Exception("LiteLLM service read timeout after multiple attempts")
                
        except httpx.HTTPStatusError as e:
            print(f"❌ HTTP error: {e.response.status_code} - {e.response.text}")
            raise Exception(f"LiteLLM API error: {e.response.status_code}")
            
        except Exception as e:
            print(f"❌ Unexpected error on attempt {attempt + 1}: {e}")
            print(f"🔍 Exception type: {type(e).__name__}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5
                print(f"⏳ Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)
            else:
                raise Exception(f"LiteLLM API call failed: {str(e)}")

def post_review_comment(repo_name, pr_number, review):
    """Post successful AI review comment"""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "AI-Code-Reviewer/1.0"
    }
    
    comment_url = f"{GITHUB_API_URL}/repos/{repo_name}/issues/{pr_number}/comments"
    ai_review = f"## 🤖 AI Code Review\n\n{review}\n\n---\n*Generated by AI Code Reviewer*"
    
    try:
        response = httpx.post(comment_url, headers=headers, json={"body": ai_review}, timeout=30.0)
        if response.status_code == 201:
            print("✅ Review posted to PR.")
        else:
            print(f"❌ Failed to post comment: Status {response.status_code}, Response: {response.text}")
    except Exception as e:
        print(f"❌ Failed to post review comment: {e}")

def post_error_comment(repo_name, pr_number, error_message):
    """Post error comment to PR"""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "AI-Code-Reviewer/1.0"
    }
    
    comment_url = f"{GITHUB_API_URL}/repos/{repo_name}/issues/{pr_number}/comments"
    error_comment = f"## ❌ AI Code Review Error\n\n{error_message}\n\n---\n*AI Code Reviewer encountered an issue*"
    
    try:
        response = httpx.post(comment_url, headers=headers, json={"body": error_comment}, timeout=30.0)
        if response.status_code == 201:
            print("✅ Error comment posted to PR.")
        else:
            print(f"❌ Failed to post error comment: Status {response.status_code}")
    except Exception as e:
        print(f"❌ Failed to post error comment: {e}")

def post_info_comment(repo_name, pr_number, info_message):
    """Post info comment to PR"""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "AI-Code-Reviewer/1.0"
    }
    
    comment_url = f"{GITHUB_API_URL}/repos/{repo_name}/issues/{pr_number}/comments"
    info_comment = f"## ℹ️ AI Code Review Info\n\n{info_message}\n\n---\n*AI Code Reviewer*"
    
    try:
        response = httpx.post(comment_url, headers=headers, json={"body": info_comment}, timeout=30.0)
        if response.status_code == 201:
            print("✅ Info comment posted to PR.")
    except Exception as e:
        print(f"❌ Failed to post info comment: {e}")

# Modified handle_review function with better error handling
def handle_review(data):
    repo_path = None
    try:
        repo_url = data["repository"]["clone_url"]
        pr_branch = data["pull_request"]["head"]["ref"]
        pr_number = data["pull_request"]["number"]
        repo_name = data["repository"]["full_name"]
        base_branch = data["pull_request"]["base"]["ref"]

        print(f"🔍 Processing PR #{pr_number} on {repo_name}")
        print(f"📊 PR branch: {pr_branch} -> {base_branch}")

        # First check if LiteLLM is accessible
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            litellm_healthy = loop.run_until_complete(check_litellm_health())
            if not litellm_healthy:
                print("❌ LiteLLM service is not accessible, aborting review")
                post_error_comment(repo_name, pr_number, "AI review service is currently unavailable. Please try again later.")
                return
        except Exception as e:
            print(f"❌ Failed to check LiteLLM health: {e}")
            post_error_comment(repo_name, pr_number, "AI review service health check failed.")
            return

        repo_path = clone_repo(repo_url, pr_branch, base_branch)
        context_code = get_repo_context(repo_path)
        diff = get_diff(repo_path, base_branch)

        if not diff.strip():
            print("⚠️ No diff found, skipping review")
            post_info_comment(repo_name, pr_number, "No changes detected for review.")
            return

        prompt = f"""
You are an experienced software engineer conducting a thorough code review. provide a sumary graph of the application along with summary in the end.

# Pull Request Information:
- Repository: {repo_name}
- PR Number: #{pr_number}
- Branch: {pr_branch} → {base_branch}

# Code Changes (Diff):
```diff
{diff}
```

# Project Context (relevant files):
{context_code[:50000]}  # Limit context to prevent token overflow

Please provide a comprehensive code review focusing on:
1. **Bugs and Issues**: Any potential bugs, logic errors, or problematic code
2. **Security Concerns**: Security vulnerabilities or best practices violations
3. **Performance**: Performance implications and optimization opportunities
4. **Code Quality**: Code style, maintainability, and best practices
5. **Suggestions**: Constructive improvements and recommendations

Format your response in GitHub markdown with clear sections and actionable feedback.
"""

        try:
            review = loop.run_until_complete(call_litellm(prompt))
            print("\n===== ✅ AI REVIEW OUTPUT =====\n")
            print(review)
            
            # Post successful review
            post_review_comment(repo_name, pr_number, review)
            
        except Exception as llm_error:
            print(f"❌ LiteLLM call failed: {llm_error}")
            error_msg = f"AI review failed due to service error: {str(llm_error)}"
            post_error_comment(repo_name, pr_number, error_msg)

    except Exception as e:
        print(f"❌ Review failed: {e}")
        import traceback
        traceback.print_exc()
        
        # Try to post error comment if we have the necessary info
        try:
            if 'repo_name' in locals() and 'pr_number' in locals():
                post_error_comment(repo_name, pr_number, f"Review process failed: {str(e)}")
        except:
            print("❌ Could not post error comment")
            
    finally:
        # Clean up
        if repo_path and os.path.exists(repo_path):
            shutil.rmtree(repo_path, ignore_errors=True)
        
        # Close the event loop
        try:
            loop.close()
        except:
            pass

# Parse GitHub webhook payload
def parse_github_payload(request):
    """Parse GitHub webhook payload handling both JSON and form-encoded data"""
    content_type = request.content_type or ""
    
    if "application/json" in content_type:
        return request.get_json()
    elif "application/x-www-form-urlencoded" in content_type:
        # GitHub sends form-encoded data with payload parameter
        form_data = request.get_data(as_text=True)
        if form_data.startswith('payload='):
            payload_data = form_data[8:]  # Remove 'payload=' prefix
            decoded_payload = urllib.parse.unquote_plus(payload_data)
            return json.loads(decoded_payload)
        else:
            raise ValueError("No payload parameter found in form data")
    else:
        raise ValueError(f"Unsupported content type: {content_type}")

# Flask webhook endpoint
@app.route("/review", methods=["POST", "GET"])
def review():
    try:
        print(f"📥 Received {request.method} request to /review")
        print(f"📋 Headers: {dict(request.headers)}")
        
        # Handle GET requests for testing
        if request.method == "GET":
            return jsonify({
                "message": "Review endpoint is working",
                "method": "POST",
                "expected_payload": "GitHub webhook payload"
            }), 200
        
        # Handle POST requests (actual webhooks)
        print(f"📦 Content-Type: {request.content_type}")
        
        # Try to parse the payload
        try:
            data = parse_github_payload(request)
            print(f"📄 Payload parsed successfully: {type(data)}")
        except Exception as parse_error:
            print(f"❌ Failed to parse payload: {parse_error}")
            print(f"📄 Raw data: {request.data[:500]}...")  # First 500 chars
            return jsonify({"error": f"Failed to parse payload: {str(parse_error)}"}), 400
        
        if not data:
            print("❌ Empty payload received")
            return jsonify({"error": "Empty payload"}), 400
        
        # Skip webhook signature verification
        print("📝 Skipping webhook signature verification")
        
        # Check if it's a GitHub webhook
        github_event = request.headers.get('X-GitHub-Event')
        print(f"🔔 GitHub Event: {github_event}")
        
        if github_event != 'pull_request':
            print(f"⚠️ Not a pull request event: {github_event}")
            return jsonify({"message": f"Ignoring event: {github_event}"}), 200
        
        # Only process pull request events
        if "pull_request" not in data or "action" not in data:
            print("⚠️ Missing pull_request or action in payload")
            print(f"📄 Available keys: {list(data.keys()) if data else 'None'}")
            return jsonify({"message": "Not a valid PR event"}), 200

        # Only process opened, synchronize (new commits), or reopened PRs
        action = data["action"]
        if action not in ["opened", "synchronize", "reopened"]:
            print(f"⚠️ Ignoring PR action: {action}")
            return jsonify({"message": f"Ignoring action: {action}"}), 200

        pr_number = data["pull_request"]["number"]
        repo_name = data["repository"]["full_name"]
        print(f"📝 Processing PR #{pr_number} in {repo_name} (action: {action})")
        
        # Start review in background thread
        threading.Thread(target=handle_review, args=(data,), daemon=True).start()
        
        return jsonify({
            "status": "processing", 
            "action": action,
            "pr_number": pr_number,
            "repository": repo_name
        }), 200

    except Exception as e:
        print(f"❌ Webhook processing error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500

@app.route("/health", methods=["GET"])
def health_check():
    print("🔍 Running health check...")
    
    # Test LiteLLM connectivity
    async def test_litellm():
        return await check_litellm_health()
    
    # Run the async health check
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    litellm_healthy = False
    litellm_error = None
    
    try:
        litellm_healthy = loop.run_until_complete(test_litellm())
    except Exception as e:
        print(f"❌ Health check error: {e}")
        litellm_error = str(e)
    finally:
        loop.close()
    
    health_status = {
        "status": "ok" if litellm_healthy else "degraded",
        "litellm_configured": bool(LITELLM_URL and LITELLM_KEY),
        "litellm_accessible": litellm_healthy,
        "github_configured": bool(GITHUB_TOKEN),
        "litellm_url": LITELLM_URL if LITELLM_URL else "Not configured",
        "model_id": MODEL_ID if MODEL_ID else "Not configured",
        "litellm_error": litellm_error
    }
    
    status_code = 200 if litellm_healthy else 503
    return jsonify(health_status), status_code

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "AI Code Review Agent",
        "endpoints": ["/review", "/health"],
        "status": "running"
    })

if __name__ == "__main__":
    # Validate required environment variables
    required_vars = ["LITELLM_URL", "LITELLM_KEY", "MODEL_ID", "GITHUB_TOKEN"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        print(f"❌ Missing required environment variables: {', '.join(missing_vars)}")
        exit(1)
    
    print("🚀 Starting AI Code Review Agent...")
    print(f"📍 Health check: http://localhost:6000/health")
    print(f"🔗 Webhook endpoint: http://localhost:6000/review")
    print(f"🔧 LiteLLM URL: {LITELLM_URL}")
    print(f"🤖 Model ID: {MODEL_ID}")
    
    app.run(host="0.0.0.0", port=6000, debug=False)