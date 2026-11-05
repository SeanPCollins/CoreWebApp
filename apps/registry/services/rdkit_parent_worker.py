import json
import sys


def _values(molecule, policy):
    from rdkit import Chem, rdBase
    from rdkit.Chem import Descriptors, rdMolDescriptors

    inchi = Chem.MolToInchi(molecule)
    inchikey = Chem.MolToInchiKey(molecule).upper()
    if not inchi or not inchikey:
        raise ValueError('RDKit could not generate an InChI and InChIKey.')
    return {
        'inchikey': inchikey,
        'inchi': inchi,
        'canonical_smiles': Chem.MolToSmiles(
            molecule,
            canonical=True,
            isomericSmiles=False,
        ),
        'isomeric_smiles': Chem.MolToSmiles(
            molecule,
            canonical=True,
            isomericSmiles=True,
        ),
        'molecular_formula': rdMolDescriptors.CalcMolFormula(molecule),
        'molecular_weight': float(Descriptors.MolWt(molecule)),
        'exact_mass': float(rdMolDescriptors.CalcExactMolWt(molecule)),
        'standardization_toolkit': 'RDKit',
        'standardization_version': rdBase.rdkitVersion,
        'standardization_policy': policy,
    }


def _process_row(row):
    from rdkit import Chem
    from rdkit.Chem.MolStandardize import rdMolStandardize

    edited_smiles = row.pop('edited_smiles', None)
    if edited_smiles is not None:
        molecule = Chem.MolFromSmiles(edited_smiles.strip())
        if molecule is None:
            row['worker_error'] = 'RDKit could not parse the edited SMILES.'
            return row, None, 'FAILED'
        row.update(_values(
            molecule,
            'RDKit canonical structure; no parent normalization',
        ))

    smiles = row.get('isomeric_smiles') or row.get('canonical_smiles')
    molecule = Chem.MolFromSmiles(smiles) if smiles else None
    if molecule is None and row.get('inchi'):
        molecule = Chem.MolFromInchi(row['inchi'])

    if molecule is None:
        parent_values = {
            field: row.get(field)
            for field in (
                'inchikey',
                'inchi',
                'canonical_smiles',
                'isomeric_smiles',
                'molecular_formula',
                'molecular_weight',
                'exact_mass',
                'standardization_toolkit',
                'standardization_version',
                'standardization_policy',
            )
        }
        return row, parent_values, 'SELF_NO_STRUCTURE'

    try:
        fragments = Chem.GetMolFrags(molecule, asMols=True)
        cleaned_fragments = []
        component_counts = {}
        for fragment in fragments:
            heavy_atoms = fragment.GetNumHeavyAtoms()
            if heavy_atoms <= 1:
                continue
            cleaned_fragment = rdMolStandardize.Cleanup(fragment)
            cleaned_fragment = rdMolStandardize.Uncharger().uncharge(
                cleaned_fragment
            )
            Chem.RemoveStereochemistry(cleaned_fragment)
            fragment_values = _values(
                cleaned_fragment,
                'RDKit cleaned non-trivial component; stereochemistry removed',
            )
            component_key = fragment_values['inchikey']
            if component_key not in component_counts:
                component_counts[component_key] = {
                    **fragment_values,
                    'component_count': 0,
                    'position': len(component_counts),
                }
            component_counts[component_key]['component_count'] += 1
            cleaned_fragments.append({
                'molecule': cleaned_fragment,
                'values': fragment_values,
                'heavy_atoms': heavy_atoms,
                'has_carbon': any(
                    atom.GetAtomicNum() == 6 for atom in fragment.GetAtoms()
                ),
            })

        organic_fragments = [
            fragment for fragment in cleaned_fragments
            if fragment['has_carbon']
        ]
        if organic_fragments:
            chosen = sorted(
                organic_fragments,
                key=lambda fragment: (
                    -fragment['heavy_atoms'],
                    fragment['values']['canonical_smiles'],
                ),
            )[0]
            parent_molecule = chosen['molecule']
            status = 'GENERATED'
            policy = (
                'RDKit cleaned largest organic fragment; '
                'stereochemistry removed'
            )
        else:
            parent_molecule = rdMolStandardize.Cleanup(molecule)
            parent_molecule = rdMolStandardize.Uncharger().uncharge(
                parent_molecule
            )
            Chem.RemoveStereochemistry(parent_molecule)
            status = 'NO_ORGANIC_COMPONENT'
            policy = (
                'RDKit cleaned full structure; no organic fragment; '
                'stereochemistry removed'
            )

        parent_values = _values(parent_molecule, policy)
        parent_values['parent_generation_status'] = status
        for component in component_counts.values():
            component['is_parent_component'] = (
                component['inchikey'] == parent_values['inchikey']
            )
        row['components'] = (
            sorted(
                component_counts.values(),
                key=lambda component: component['position'],
            )
            if len(fragments) > 1
            else []
        )
        return row, parent_values, status
    except (RuntimeError, ValueError) as exc:
        return row, None, str(exc)[:300]


def process_files(source_path, output_path, parent_path):
    parent_records = {}
    with open(source_path, encoding='utf-8') as source, open(
        output_path,
        'w',
        encoding='utf-8',
    ) as output:
        for line in source:
            if not line.strip():
                continue
            row, parent_values, status = _process_row(json.loads(line))
            if row.get('worker_error'):
                output.write(json.dumps(row, ensure_ascii=False) + '\n')
                continue
            parent_key = parent_values.get('inchikey') if parent_values else row['inchikey']
            row.update({
                'parent_id': None,
                'parent_inchikey': parent_key,
                'parent_is_self': parent_key == row['inchikey'],
                'parent_generation_status': (
                    'FAILED' if parent_values is None else status
                ),
                'parent_generation_error': (
                    status if parent_values is None else None
                ),
            })
            if parent_values is None:
                row['review_flags'] = sorted(set(
                    row.get('review_flags', []) + ['parent_generation_failed']
                ))
                row['review_status'] = 'REVIEW'
            else:
                parent_records.setdefault(parent_key, {
                    **parent_values,
                    'parent_id': None,
                    'parent_inchikey': parent_key,
                    'parent_is_self': True,
                    'parent_generation_status': status,
                    'parent_generation_error': None,
                    'already_in_structure_database': False,
                })
            output.write(json.dumps(row, ensure_ascii=False) + '\n')

    with open(parent_path, 'w', encoding='utf-8') as parent_file:
        for parent_key in sorted(parent_records):
            parent_file.write(
                json.dumps(parent_records[parent_key], ensure_ascii=False) + '\n'
            )


def _salt_component_signature(smiles):
    from collections import defaultdict

    from rdkit import Chem
    from rdkit.Chem.MolStandardize import rdMolStandardize

    molecule = Chem.MolFromSmiles(smiles) if smiles else None
    if molecule is None:
        return None

    fragments = Chem.GetMolFrags(molecule, asMols=True)
    if not fragments:
        return None
    normalized_fragments = []
    for fragment in fragments:
        cleaned = rdMolStandardize.Cleanup(fragment)
        cleaned = rdMolStandardize.Uncharger().uncharge(cleaned)
        stereo_free = Chem.Mol(cleaned)
        Chem.RemoveStereochemistry(stereo_free)
        canonical_smiles = Chem.MolToSmiles(
            stereo_free,
            canonical=True,
            isomericSmiles=False,
        )
        normalized_fragments.append({
            'key': Chem.MolToInchiKey(cleaned).upper(),
            'stereo_free_key': Chem.MolToInchiKey(stereo_free).upper(),
            'stereo_free_smiles': canonical_smiles,
            'heavy_atoms': fragment.GetNumHeavyAtoms(),
            'has_carbon': any(
                atom.GetAtomicNum() == 6 for atom in fragment.GetAtoms()
            ),
        })

    organic_indices = [
        index for index, fragment in enumerate(normalized_fragments)
        if fragment['has_carbon'] and fragment['heavy_atoms'] > 1
    ]
    if not organic_indices:
        return None
    parent_index = sorted(
        organic_indices,
        key=lambda index: (
            -normalized_fragments[index]['heavy_atoms'],
            normalized_fragments[index]['stereo_free_smiles'],
        ),
    )[0]
    parent_key = normalized_fragments[parent_index]['key']
    if not parent_key or any(
        not fragment['key'] or not fragment['stereo_free_key']
        for fragment in normalized_fragments
    ):
        return None

    organic_components = defaultdict(int)
    salt_components = defaultdict(int)
    for index, fragment in enumerate(normalized_fragments):
        if index == parent_index:
            continue
        target = (
            organic_components
            if fragment['has_carbon']
            else salt_components
        )
        target[fragment['key']] += 1
    return [
        parent_key,
        dict(organic_components),
        dict(salt_components),
    ]


def process_salt_signatures(source_path, output_path):
    with open(source_path, encoding='utf-8') as source, open(
        output_path,
        'w',
        encoding='utf-8',
    ) as output:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            output.write(json.dumps({
                'signature': _salt_component_signature(row.get('smiles')),
            }) + '\n')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--salt-signatures':
        process_salt_signatures(sys.argv[2], sys.argv[3])
    else:
        process_files(sys.argv[1], sys.argv[2], sys.argv[3])
