#!/usr/bin/env python

import time
from chembl_webresource_client.new_client import new_client
from chembl_webresource_client.settings import Settings

# Configure fast failure and caching
settings = Settings.Instance()
settings.TIMEOUT = 5
settings.CACHING = True

molecule = new_client.molecule

def test_form(category, query_kwargs):
    print(f"\n[TEST] Testing Form: {category}")
    print(f"       Query Params: {query_kwargs}")
    start = time.time()
    try:
        # Execute query and restrict fields to molecule_chembl_id
        query = molecule.filter(**query_kwargs).only('molecule_chembl_id')
        results = list(query)
        elapsed = time.time() - start
        
        ids = [r.get('molecule_chembl_id') for r in results]
        print(f"       Time: {elapsed:.2f}s | Found: {len(ids)} records")
        print(f"       Mapped ChEMBL IDs: {ids[:5]}")
    except Exception as e:
        print(f"       FAILED ({time.time() - start:.2f}s) | Error: {e}")

if __name__ == "__main__":
    # 1. Native ChEMBL ID
    test_form(
        "Direct ChEMBL ID", 
        {"chembl_id": "CHEMBL25"}
    )

    # 2. Preferred Name (Exact Match)
    test_form(
        "Preferred Name (__iexact)", 
        {"pref_name__iexact": "aspirin"}
    )

    # 3. Synonym Table (Exact Match)
    test_form(
        "Synonym Table (__iexact)", 
        {"molecule_synonyms__molecule_synonym__iexact": "acetylsalicylic acid"}
    )

    # 4. Standard InChIKey
    test_form(
        "Standard InChIKey", 
        {"molecule_structures__standard_inchi_key": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"}
    )

    # 5. Canonical SMILES
    test_form(
        "Canonical SMILES", 
        {"molecule_structures__canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O"}
    )
