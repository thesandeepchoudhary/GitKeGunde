def build_mcp_context(mr_metadata, diffs, files):
    return {
        "version": "1.0",
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful code reviewer."
            },
            {
                "role": "user",
                "name": mr_metadata.get("author", {}).get("username", "user"),
                "content": f"Please review this merge request titled: {mr_metadata.get('title')}"
            },
            {
                "role": "tool",
                "name": "gitlab-pr-context",
                "content": {
                    "repo": mr_metadata.get("web_url", ""),
                    "mr_title": mr_metadata.get("title"),
                    "mr_description": mr_metadata.get("description"),
                    "diffs": diffs,
                    "full_files": files
                }
            }
        ]
    }