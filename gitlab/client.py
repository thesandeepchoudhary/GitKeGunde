import requests

class GitLabClient:
    def __init__(self, token: str):
        self.token = token
        self.headers = {"PRIVATE-TOKEN": token}
        self.base_url = "https://gitlab.com/api/v4"

    def get_merge_request_diff(self, project_id, mr_iid):
        url = f"{self.base_url}/projects/{project_id}/merge_requests/{mr_iid}/changes"
        return requests.get(url, headers=self.headers).json()

    def get_merge_request_files(self, project_id, mr_iid):
        # Fetch all changed files (optional implementation)
        return []

    def get_merge_request_metadata(self, project_id, mr_iid):
        url = f"{self.base_url}/projects/{project_id}/merge_requests/{mr_iid}"
        return requests.get(url, headers=self.headers).json()

    def post_comment(self, project_id, mr_iid, body):
        url = f"{self.base_url}/projects/{project_id}/merge_requests/{mr_iid}/notes"
        return requests.post(url, headers=self.headers, json={"body": body})