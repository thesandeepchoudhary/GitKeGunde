def extract_code_changes(diff_data):
    diffs = 
    for change in diff_data.get("changes", []):
        diffs.append({
            "file": change["new_path"],
            "diff": change["diff"]
        })
    return diffs
