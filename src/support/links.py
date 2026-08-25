def format_issue_links(issue_links: list, key_map: dict) -> list:
    """Zephyr issue-link objects → markdown parts.

    Resolved ids become ``[KEY](https://site/browse/KEY)`` (site derived from
    the link's REST ``target``); unresolved ones fall back to the target URL.
    """
    parts = []
    for link in issue_links:
        iid = str(link.get("issueId"))
        key = key_map.get(iid)
        if key:
            root = (link.get("target") or "").split("/rest/")[0]
            parts.append(f"[{key}]({root}/browse/{key})" if root else key)
        else:
            parts.append(link.get("target") or f"issueId {iid}")
    return parts
