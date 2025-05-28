def post_review_comment(client, project_id, mr_iid, comment):
    client.post_comment(project_id, mr_iid, comment)