import requests

def query_crossref_by_title(title, rows=3):
    url = "https://api.crossref.org/works"
    params = {
        "query.bibliographic": title,
        "rows": rows
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data["message"]["items"]

# 示例论文标题
title = "Certified data removal from machine learning models Proceedings of the 37th International Conference on Machine Learning"

results = query_crossref_by_title(title)

for item in results:
    print("=" * 50)
    print("Title:", item.get("title", [""])[0])
    print("DOI:", item.get("DOI"))
    print("Year:", item.get("issued", {}).get("date-parts", [[None]])[0][0])
    print("Venue:", item.get("container-title", [""])[0])
