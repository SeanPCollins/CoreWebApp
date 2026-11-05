import json
import os

def interactive_tree_curator(filepath):
    if not os.path.exists(filepath):
        print(f"[-] Error: Could not find '{filepath}'. Make sure your sample JSON is in the same directory.")
        return

    print(f"[*] Loading '{filepath}' and indexing data nodes...\n")
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    record = data.get('Record', {})
    title = record.get('RecordTitle', 'Unknown')
    
    curation_items = []

    def extract_clean_texts(info_list):
        """Extracts readable text strings from PubChem's nested markup and value structures."""
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

    def collect_walk(sections, parent_path=""):
        for sec in sections:
            heading = sec.get('TOCHeading', 'Unnamed Section')
            current_path = f"{parent_path} > {heading}" if parent_path else heading
            
            info_list = sec.get('Information', [])
            sub_sections = sec.get('Section', [])
            
            clean_snippets = extract_clean_texts(info_list)
            
            if clean_snippets:
                curation_items.append({
                    'path': current_path,
                    'snippets': clean_snippets
                })
                
            if sub_sections:
                collect_walk(sub_sections, current_path)

    collect_walk(record.get('Section', []))
    
    total_items = len(curation_items)
    print(f"Chemical Record Loaded: {title}")
    print(f"Found {total_items} distinct data sections to review.")
    print("Instructions:")
    print("  [y] Save to KEEP list")
    print("  [n] Send to IGNORE list")
    print("  [v] View all detailed text items in this section\n")
    print("=" * 70)

    saved_paths = []
    ignored_paths = []

    for idx, item in enumerate(curation_items, start=1):
        current_path = item['path']
        snippets = item['snippets']
        
        sample_preview = " | ".join(snippets[:3])
        if len(sample_preview) > 150:
            sample_preview = sample_preview[:150] + "..."

        print(f"\n[{idx} of {total_items}] Path: {current_path}")
        print(f"Data Sample: {sample_preview if sample_preview else '(No preview text available)'}")
        
        while True:
            choice = input("Choice [y/n/v]: ").strip().lower()
            if choice in ['y', 'yes']:
                saved_paths.append(current_path)
                print("  --> Saved to KEEP list.")
                break
            elif choice in ['n', 'no']:
                ignored_paths.append(current_path)
                print("  --> Sent to IGNORE list.")
                break
            elif choice in ['v', 'view']:
                print(f"\n--- Full Details for: {current_path} ---")
                for s_idx, snip in enumerate(snippets, start=1):
                    print(f"  [{s_idx}] {snip}")
                print("-" * 50)
                # Loop continues back to the prompt after viewing
            else:
                print("Please enter 'y' (save), 'n' (ignore), or 'v' (view details).")

    # Save output configuration files
    with open('pubchem_saved_paths.json', 'w', encoding='utf-8') as f:
        json.dump(saved_paths, f, indent=2)
        
    with open('pubchem_ignored_paths.json', 'w', encoding='utf-8') as f:
        json.dump(ignored_paths, f, indent=2)

    print("\n" + "=" * 70)
    print("[+] Curation complete!")
    print(f"[+] Saved paths recorded: {len(saved_paths)} -> written to 'pubchem_saved_paths.json'")
    print(f"[+] Ignored paths recorded: {len(ignored_paths)} -> written to 'pubchem_ignored_paths.json'")

if __name__ == "__main__":
    interactive_tree_curator("pubchem_dump_cid_241.json")
