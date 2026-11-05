#!/usr/bin/env python

import requests
import json

def test_pubchem_full_dump(term="benzene"):
    print(f"[*] Step 1: Resolving '{term}' to a PubChem CID...")
    
    # Step 1: Get CID via PUG REST
    # Note: PubChem handles names, synonyms, and CAS numbers seamlessly here
    rest_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{term}/cids/json"
    
    response = requests.get(rest_url, timeout=10)
    if response.status_code != 200:
        print(f"[-] Failed to find CID for '{term}'. HTTP Status: {response.status_code}")
        return
    
    data = response.json()
    try:
        cids = data.get('IdentifierList', {}).get('CID', [])
        if not cids:
            print(f"[-] No CIDs found for '{term}'.")
            return
        
        target_cid = cids[0]
        print(f"[+] Found CID: {target_cid} (Total matches: {len(cids)})")
        
    except Exception as e:
        print(f"[-] Error parsing CID response: {e}")
        return

    print(f"\n[*] Step 2: Fetching FULL Master Record via PUG View for CID {target_cid}...")
    
    # Step 2: Pull the entire record hierarchy using PUG View JSON endpoint
    view_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{target_cid}/JSON"
    
    view_response = requests.get(view_url, timeout=15)
    if view_response.status_code != 200:
        print(f"[-] Failed to fetch PUG View payload. HTTP Status: {view_response.status_code}")
        return
        
    full_payload = view_response.json()
    
    # Inspecting the shape of the payload
    record = full_payload.get('Record', {})
    record_title = record.get('RecordTitle', 'Unknown')
    record_source = record.get('RecordSource', 'Unknown')
    sections = record.get('Section', [])
    
    print(f"\n[+] Successfully fetched full record for: {record_title}")
    print(f"[+] Source authority: {record_source}")
    print(f"[+] Total Top-Level Sections in Payload: {len(sections)}")
    
    print("\n--- Top-Level Sections Available in PUG View ---")
    for sec in sections:
        sec_heading = sec.get('TOCHeading', 'Unnamed Section')
        print(f"    • {sec_heading}")

    # Print a tiny sample output file size
    payload_string = json.dumps(full_payload)
    print(f"\n[+] Total payload size: {len(payload_string):,} characters of raw JSON data.")
    
    # Optional: Save it locally so you can inspect the file structure
    filename = f"pubchem_dump_cid_{target_cid}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(full_payload, f, indent=2)
    print(f"[+] Saved complete raw payload locally to '{filename}' for structural inspection.")

if __name__ == "__main__":
    # Test with a common chemical name or CAS
    test_pubchem_full_dump("benzene")
