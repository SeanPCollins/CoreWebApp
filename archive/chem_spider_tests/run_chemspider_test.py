import os
import pickle
from django.conf import settings
from chemspipy import ChemSpider
from openbabel import openbabel as ob
from openbabel import pybel

# Silence OpenBabel C++ core warning output
ob.obErrorLog.SetOutputLevel(0)

CACHE_FILE = "chemspider_test_cache.pkl"
test_csids = [17215774, 21156958, 74652, 8106]

print("==================================================")
print(" ChemSpider Caching & Sandbox Test Script")
print("==================================================")

batch_results = None

# 1. Check if we have a local cache file already
if os.path.exists(CACHE_FILE):
    print(f"[CACHE HIT] Found existing cache file '{CACHE_FILE}'.")
    print("[CACHE HIT] Loading objects locally from disk -> 0 API calls used!")
    with open(CACHE_FILE, "rb") as f:
        batch_results = pickle.load(f)
else:
    print(f"[CACHE MISS] No local cache found at '{CACHE_FILE}'.")
    print("[CACHE MISS] Querying live ChemSpider API (Batch Details) -> 1 API call used...")

    api_key = getattr(settings, 'CHEM_SPIDER_API_KEY', '')
    if not api_key:
        print("[ERROR] CHEM_SPIDER_API_KEY is missing or empty in settings!")
    else:
        cs = ChemSpider(api_key)
        optimal_fields = [
            'SMILES', 'Formula', 'MolecularWeight', 'CommonName', 'Mol2D'
        ]

        try:
            batch_results = cs.get_details_batch(test_csids, fields=optimal_fields)
            
            with open(CACHE_FILE, "wb") as f:
                pickle.dump(batch_results, f)
            print(f"[CACHE STORE] Successfully fetched and cached records to '{CACHE_FILE}'.")
        except Exception as e:
            print(f"[API ERROR] Failed to fetch batch from ChemSpider: {e}")

# 2. Process results and map against requested IDs to handle missing entries
if batch_results is not None:
    print(f"\n--- Processing Batch Results ({len(batch_results)} records returned) ---")
    
    # Map returned records by their ID for easy lookup
    returned_map = {}
    for record in batch_results:
        csid = (
            record.get('id') or 
            record.get('recordId') or 
            record.get('record_id') or 
            record.get('identifier') or 
            record.get('csid')
        )
        if csid:
            returned_map[int(csid)] = record

    # Evaluate all requested IDs
    for idx, req_id in enumerate(test_csids, 1):
        if req_id in returned_map:
            record = returned_map[req_id]
            common_name = record.get('commonName')
            formula = record.get('formula')
            mw = record.get('molecularWeight')
            smiles = record.get('smiles')
            mol2d_str = record.get('mol2D')

            print(f"\n[Record {idx}] CSID: {req_id} | Name: {common_name}")
            print(f"  - Formula:          {formula}")
            print(f"  - Molecular Weight: {mw}")
            print(f"  - SMILES:           {smiles}")

            if mol2d_str:
                try:
                    mol = pybel.readstring('mol', mol2d_str)
                    inchi = mol.write('inchi').strip()
                    inchikey = mol.write('inchikey').strip()
                    print(f"  - Computed InChI:     {inchi[:40]}...")
                    print(f"  - Computed InChIKey:  {inchikey}")
                except Exception as conv_err:
                    print(f"  - Local conversion error: {conv_err}")
            else:
                print("  - Mol2D string missing from record payload.")
        else:
            print(f"\n[Record {idx}] CSID: {req_id} -> [NOT FOUND] ID was not returned in batch results.")

    print("\n==================================================")
    print(" Test completed successfully.")
    print("==================================================")
else:
    print("[ERROR] No batch results were loaded or retrieved.")
