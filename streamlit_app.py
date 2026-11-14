import difflib
import io
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import streamlit as st
from pypdf import PdfReader, PdfWriter

ROOT_FOLDER_ID = "0B4fd8LwUUi4YYzJ2ZDFwRzJZTlk"
RESOURCE_KEY = "0-A7jhk6BuC3Tdr-PDudWCTg"  # the value after resourcekey= in the share link
SETLISTS_FOLDER_ID = "0B4fd8LwUUi4YakNkSmRieWpmX00?"
SETLISTS_RESOURCE_KEY = "0-NtQccyAtb4uVjS26BHrgTA&q"  # only if the set lists folder link has a resourcekey
API_KEY = st.secrets["drive_api_key"]
API = "https://www.googleapis.com/drive/v3/files"

FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

st.set_page_config(page_title="Music book builder", layout="wide")


def rk_headers(*pairs):
    """Build the resource-key header from (item_id, key) pairs, skipping empty keys."""
    value = ",".join(f"{i}/{k}" for i, k in pairs if k)
    return {"X-Goog-Drive-Resource-Keys": value} if value else {}


def list_all(query, fields, headers):
    items, token = [], None
    while True:
        params = {
            "q": query,
            "fields": f"nextPageToken, files({fields})",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "key": API_KEY,
        }
        if token:
            params["pageToken"] = token
        t = time.time()
        r = requests.get(API, params=params, headers=headers, timeout=30)
        print(f"Drive request: {r.status_code} in {time.time() - t:.1f}s", flush=True)
        if not r.ok:
            raise RuntimeError(f"Drive API error {r.status_code}: {r.text}")
        data = r.json()
        items += data["files"]
        token = data.get("nextPageToken")
        if not token:
            return items


@st.cache_data(ttl=600, show_spinner="Reading Drive...")
def load_library():
    t0 = time.time()
    root_headers = rk_headers((ROOT_FOLDER_ID, RESOURCE_KEY))
    folders = list_all(
        f"'{ROOT_FOLDER_ID}' in parents "
        f"and mimeType='{FOLDER_MIME}' and trashed=false",
        "id,name,resourceKey",
        root_headers,
    )
    folders = [f for f in folders if f["id"] != SETLISTS_FOLDER_ID]

    def folder_files(folder):
        headers = rk_headers(
            (ROOT_FOLDER_ID, RESOURCE_KEY), (folder["id"], folder.get("resourceKey"))
        )
        files = list_all(
            f"'{folder['id']}' in parents and trashed=false",
            "id,name,mimeType,resourceKey",
            headers,
        )
        return folder, files

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(folder_files, folders))

    rows, skipped, no_separator = [], [], []
    for folder, files in results:
        song = folder["name"]
        for f in files:
            if not f["name"].lower().endswith(".pdf"):
                skipped.append((song, f["name"], f["mimeType"]))
                continue
            stem = re.sub(r"\.pdf$", "", f["name"], flags=re.I)
            pieces = re.split(r"\s+[-–—]\s+", stem)
            if len(pieces) < 2:
                no_separator.append((song, f["name"]))
                continue
            rows.append(
                {
                    "song": song,
                    "part": pieces[-1].strip(),
                    "file_id": f["id"],
                    "resource_key": f.get("resourceKey") or "",
                }
            )

    per_folder = Counter(len(files) for _, files in results)
    diag = {
        "folders": len(folders),
        "folders_with_resource_key": sum(1 for f in folders if f.get("resourceKey")),
        "files_total": sum(len(files) for _, files in results),
        "parsed": len(rows),
        "files_with_resource_key": sum(1 for r in rows if r["resource_key"]),
        "files_per_folder": dict(sorted(per_folder.items())),
        "empty_folders": [f["name"] for f, files in results if not files][:15],
        "empty_folder_count": sum(1 for _, files in results if not files),
        "skipped_count": len(skipped),
        "no_separator": no_separator[:15],
        "seconds": round(time.time() - t0, 1),
    }
    print(diag, flush=True)
    return (
        pd.DataFrame(rows, columns=["song", "part", "file_id", "resource_key"]),
        diag,
    )


def doc_date(label):
    """yyyy-mm-dd from the doc's own title (the last part of its path), or ''."""
    m = DATE_RE.search(label.split(" / ")[-1])
    return m.group(0) if m else ""


@st.cache_data(ttl=600, show_spinner="Reading set lists...")
def load_setlists():
    docs, other, folders_seen = [], Counter(), [0]

    def walk(folder_id, path, keys, depth):
        folders_seen[0] += 1
        items = list_all(
            f"'{folder_id}' in parents and trashed=false",
            "id,name,mimeType,resourceKey,shortcutDetails",
            rk_headers(*keys),
        )
        for it in items:
            mime = it["mimeType"]
            label = " / ".join(path + [it["name"]])
            item_keys = keys + ((it["id"], it.get("resourceKey")),)
            if mime == FOLDER_MIME:
                if depth < 3:
                    walk(it["id"], path + [it["name"]], item_keys, depth + 1)
            elif mime == DOC_MIME:
                docs.append({"id": it["id"], "name": label, "keys": item_keys})
            elif mime == SHORTCUT_MIME:
                sd = it.get("shortcutDetails") or {}
                if sd.get("targetMimeType") == DOC_MIME:
                    target_keys = keys + ((sd["targetId"], sd.get("targetResourceKey")),)
                    docs.append(
                        {"id": sd["targetId"], "name": label, "keys": target_keys}
                    )
                else:
                    other[f"shortcut to {sd.get('targetMimeType')}"] += 1
            else:
                other[mime] += 1

    walk(
        SETLISTS_FOLDER_ID,
        [],
        (
            (ROOT_FOLDER_ID, RESOURCE_KEY),
            (SETLISTS_FOLDER_ID, SETLISTS_RESOURCE_KEY),
        ),
        0,
    )

    dated = sorted(
        [d for d in docs if doc_date(d["name"])],
        key=lambda d: (doc_date(d["name"]), d["name"].lower()),
        reverse=True,
    )
    undated = sorted(
        [d for d in docs if not doc_date(d["name"])],
        key=lambda d: d["name"].lower(),
    )
    diag = {
        "google_docs_found": len(docs),
        "undated": len(undated),
        "folders_searched": folders_seen[0],
        "skipped_other_types": dict(other),
    }
    return dated + undated, diag


@st.cache_data(ttl=600, show_spinner="Reading set list...")
def fetch_doc_text(doc_id, keys):
    r = requests.get(
        f"{API}/{doc_id}/export",
        params={"mimeType": "text/plain", "key": API_KEY},
        headers=rk_headers(*keys),
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Drive API error {r.status_code}: {r.text}")
    r.encoding = "utf-8"
    return r.text.lstrip("\ufeff")


def norm(s):
    s = s.lower().replace("&", " and ")
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def clean_line(line):
    line = line.strip().lstrip("\ufeff")
    line = re.sub(r"^\s*(?:\d+\s*[.):-]\s*|[-•*·]\s+)", "", line)
    return line.strip()


def match_song(line, index):
    """Return the song name for a set list line, or None. Exact match first, then fuzzy."""
    candidates = [
        line,
        re.sub(r"[\(\[].*?[\)\]]", "", line),
        re.split(r"\s+[-–—]\s+", line)[0],
    ]
    for c in candidates:
        n = norm(c)
        if n and n in index:
            return index[n]
    best, best_score = None, 0.0
    for c in candidates:
        n = norm(c)
        if not n:
            continue
        close = difflib.get_close_matches(n, list(index), n=1, cutoff=0.85)
        if close:
            score = difflib.SequenceMatcher(None, n, close[0]).ratio()
            if score > best_score:
                best, best_score = index[close[0]], score
    return best


def download_pdf(file_id, resource_key):
    r = requests.get(
        f"{API}/{file_id}",
        params={"alt": "media", "supportsAllDrives": "true", "key": API_KEY},
        headers=rk_headers((ROOT_FOLDER_ID, RESOURCE_KEY), (file_id, resource_key)),
        timeout=60,
    )
    r.raise_for_status()
    return io.BytesIO(r.content)


def build_book(selected: pd.DataFrame) -> bytes:
    bar = st.progress(0.0, text="Downloading...")
    with ThreadPoolExecutor(max_workers=6) as pool:
        buffers = []
        for i, buf in enumerate(
            pool.map(download_pdf, selected["file_id"], selected["resource_key"]),
            start=1,
        ):
            buffers.append(buf)
            bar.progress(i / len(selected), text=f"Downloaded {i}/{len(selected)}")

    writer = PdfWriter()
    for song, buf in zip(selected["song"], buffers):
        writer.append(PdfReader(buf), outline_item=song)
    out = io.BytesIO()
    writer.write(out)
    bar.empty()
    return out.getvalue()


st.title("Music book builder")

try:
    lib, diag = load_library()
except RuntimeError as e:
    st.error(str(e))
    st.stop()

with st.expander("Diagnostics", expanded=lib.empty):
    st.write(diag)

if lib.empty:
    st.warning("No parts found. See Diagnostics above.")
    st.stop()

part_counts = lib["part"].value_counts()
parts = sorted(part_counts.index, key=str.lower)
part = st.selectbox(
    "Part",
    parts,
    index=parts.index(part_counts.idxmax()),
    format_func=lambda p: f"{p} ({part_counts[p]} songs)",
)
available = lib[lib["part"] == part].sort_values("song", key=lambda s: s.str.lower())

mode = st.radio("Build from", ["Pick songs", "Set list"], horizontal=True)

if mode == "Pick songs":
    table = pd.DataFrame(
        {
            "Include": False,
            "Order": range(1, len(available) + 1),
            "Song": available["song"].values,
        },
        index=available.index,
    )
    edited = st.data_editor(
        table,
        key=f"editor_pick_{part}",
        hide_index=True,
        disabled=["Song"],
        column_config={
            "Include": st.column_config.CheckboxColumn(),
            "Order": st.column_config.NumberColumn(min_value=1, step=1),
        },
    )
    chosen = edited[edited["Include"]].sort_values("Order", kind="stable")
    selected = available.loc[chosen.index]

    missing = sorted(set(lib["song"]) - set(available["song"]))
    if missing:
        with st.expander(f"{len(missing)} songs have no '{part}' part"):
            st.write(missing)

else:
    try:
        docs, setlist_diag = load_setlists()
    except RuntimeError as e:
        st.error(str(e))
        st.stop()

    with st.expander("Set list diagnostics", expanded=not docs):
        st.write(setlist_diag)
    if not docs:
        st.warning("No Google Docs found in the set lists folder.")
        st.stop()

    doc = docs[
        st.selectbox(
            "Set list", range(len(docs)), format_func=lambda i: docs[i]["name"]
        )
    ]
    try:
        text = fetch_doc_text(doc["id"], doc["keys"])
    except RuntimeError as e:
        st.error(str(e))
        st.stop()

    index = {norm(s): s for s in lib["song"].unique()}
    matched, unmatched = [], []
    for raw in text.splitlines():
        line = clean_line(raw)
        if not line:
            continue
        song = match_song(line, index)
        if song:
            matched.append((line, song))
        else:
            unmatched.append(line)

    by_song = available.drop_duplicates("song").set_index("song")
    table = pd.DataFrame(
        [
            {
                "Include": song in by_song.index,
                "Order": n,
                "Song": song,
                "Doc line": line,
                "Has part": song in by_song.index,
            }
            for n, (line, song) in enumerate(matched, start=1)
        ],
        columns=["Include", "Order", "Song", "Doc line", "Has part"],
    )
    edited = st.data_editor(
        table,
        key=f"editor_setlist_{doc['id']}_{part}",
        hide_index=True,
        disabled=["Song", "Doc line", "Has part"],
        column_config={
            "Include": st.column_config.CheckboxColumn(),
            "Order": st.column_config.NumberColumn(min_value=1, step=1),
        },
    )
    chosen = edited[edited["Include"] & edited["Has part"]].sort_values(
        "Order", kind="stable"
    )
    selected = by_song.loc[chosen["Song"]].reset_index()

    no_part = [s for s in table.loc[~table["Has part"], "Song"]]
    if no_part:
        st.warning(f"No '{part}' part for: " + ", ".join(no_part))
    if unmatched:
        with st.expander(f"{len(unmatched)} lines didn't match a song"):
            st.write(unmatched)

st.caption(f"{len(selected)} songs selected")

if st.button("Build PDF", disabled=selected.empty, type="primary"):
    st.session_state["book"] = (part, build_book(selected))

if "book" in st.session_state:
    book_part, data = st.session_state["book"]
    st.download_button(
        "Download book",
        data=data,
        file_name=f"Music book - {book_part}.pdf",
        mime="application/pdf",
    )
