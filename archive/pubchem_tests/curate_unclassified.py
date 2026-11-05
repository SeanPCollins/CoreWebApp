#!/usr/bin/env python

import json
import os

def curate_unclassified_paths(filepath="pubchem_dump_cid_241.json"):
    # POINT TO ROOT DIRECTORY WHERE YOUR CURRENT JSON FILES LIVE
    config_dir = "." 
    saved_file = os.path.join(config_dir, "pubchem_saved_paths.json")
    ignored_file = os.path.join(config_dir, "pubchem_ignored_paths.json")

    if not os.path.exists(filepath):
        print(f"[-] Error: Could not find '{filepath}'.")
        return

    # Load existing configuration maps
    saved_paths = json.load(open(saved_file, 'r', encoding='utf-8')) if os.path.exists(saved_file) else []
    ignored_paths = json.load(open(ignored_file, 'r', encoding='utf-8')) if os.path.exists(ignored_file) else []
    
    print(f"[*] Loaded {len(saved_paths)} saved paths and {len(ignored_paths)} ignored paths from root.")

    saved_set = set(saved_paths)
    ignored_set = set(ignored_paths)

    print(f"[*] Loading '{filepath}'...")
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    record = data.get('Record', {})
    
    def extract_clean_texts(info_list):
        clean_texts = []
        for info in info_list:
            if 'StringWithMarkup' in info:
                for item in info['StringWithMarkup']:
                    if isinstance(item, dict) and 'String' in item:
                        text = item['String'].strip()
                        if text:
                            clean_texts.append(text)
            if 'Value' in info:
                val = info['Value']
                if isinstance(val, (str, int, float)):
                    clean_texts.append(str(val))
                elif isinstance(val, dict):
                    for v_val in val.values():
                        if isinstance(v_val, (str, int, float)):
                            clean_texts.append(str(v_val))
                        elif isinstance(v_val, list):
                            clean_texts.extend([str(v) for v in v_val if v])
            for key in ['Name', 'String']:
                if key in info and isinstance(info[key], str):
                    clean_texts.append(info[key].strip())
        return [t for t in clean_texts if t]

    unclassified_items = []

    def walk_and_collect(sections, parent_path=""):
        for sec in sections:
            heading = sec.get('TOCHeading', 'Unnamed Section')
            current_path = f"{parent_path} > {heading}" if parent_path else heading
            
            if current_path not in saved_set and current_path not in ignored_set:
                snippets = extract_clean_texts(sec.get('Information', []))
                unclassified_items.append({
                    'path': current_path,
                    'snippets': snippets
                })
            
            sub_sections = sec.get('Section', [])
            if sub_sections:
                walk_and_collect(sub_sections, current_path)

    walk_and_collect(record.get('Section', []))

    total_unclassified = len(unclassified_items)
    if total_unclassified == 0:
        print("[+] Success! No unclassified paths found. Everything is fully categorized.")
        return

    print(f"\n[!] Found {total_unclassified} unclassified paths to review.")
    print("Instructions: [y] Save | [n] Ignore | [v] View details\n")
    print("=" * 70)

    new_saved = []
    new_ignored = []

    for idx, item in enumerate(unclassified_items, start=1):
        path = item['path']
        snippets = item['snippets']
        
        sample_preview = " | ".join(snippets[:3]) if snippets else "(Container section / No direct text snippets)"
        if len(sample_preview) > 150:
            sample_preview = sample_preview[:150] + "..."

        print(f"\n[{idx} of {total_unclassified}] Path: {path}")
        print(f"Data Sample: {sample_preview}")

        while True:
            choice = input("Choice [y/n/v]: ").strip().lower()
            if choice in ['y', 'yes']:
                new_saved.append(path)
                saved_set.add(path)
                print("  --> Added to SAVED list.")
                break
            elif choice in ['n', 'no']:
                new_ignored.append(path)
                ignored_set.add(path)
                print("  --> Added to IGNORED list.")
                break
            elif choice in ['v', 'view']:
                print(f"\n--- Full Details for: {path} ---")
                if snippets:
                    for s_idx, snip in enumerate(snippets, start=1):
                        print(f"  [{s_idx}] {snip}")
                else:
                    print("  (Parent container node with no direct underlying text values.)")
                print("-" * 50)
            else:
                print("Please enter 'y', 'n', or 'v'.")

    if new_saved:
        saved_paths.extend(new_saved)
        with open(saved_file, 'w', encoding='utf-8') as f:
            json.dump(saved_paths, f, indent=2)
            
    if new_ignored:
        ignored_paths.extend(new_ignored)
        with open(ignored_file, 'w', encoding='utf-8') as f:
            json.dump(ignored_paths, f, indent=2)

    print("\n" + "=" * 70)
    print(f"[+] Added {len(new_saved)} saved and {len(new_ignored)} ignored paths.")

if __name__ == "__main__":
    curate_unclassified_paths()
