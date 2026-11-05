import re

re_excel_date = re.compile(r'^(?:19|20)\d{2}-(\d{2})-(\d{2})$')

def validate_cas_checksum(cas_str):
    """
    Validates a CAS-RN using the official checksum algorithm.
    Format: N...N-NN-N
    Example: 50-02-2 -> digits: 5, 0, 0, 2 -> check digit: 2
    """
    if not cas_str or not isinstance(cas_str, str):
        return False

    cleaned = cas_str.strip()
    match = re.match(r'^(\d+)-(\d{2})-(\d)$', cleaned)
    if not match:
        return False

    group1, group2, check_digit = match.groups()
    digits = group1 + group2

    total = 0
    length = len(digits)
    for idx, char in enumerate(digits):
        weight = length - idx
        total += int(char) * weight

    return (total % 10) == int(check_digit)


def detect_and_suggest_cas(val_str):
    """
    Detects valid CAS, Excel date-corrupted CAS (YYYY-MM-DD), 
    and HCIDs (CAS with hyphens removed, e.g., 50022 -> 50-02-2).
    """
    if not val_str:
        return {'is_valid': False, 'status': 'EMPTY', 'suggestion': None, 'original': val_str}

    cleaned = str(val_str).strip()

    # 1. Check if it's already a valid standard CAS
    if validate_cas_checksum(cleaned):
        return {'is_valid': True, 'status': 'VALID', 'suggestion': cleaned, 'original': cleaned}

    # 2. Check for HCID pattern (digits only, length 5 to 10)
    if cleaned.isdigit() and 5 <= len(cleaned) <= 10:
        # Last digit is check digit, previous 2 digits are group2, remainder is group1
        check_digit = cleaned[-1]
        group2 = cleaned[-3:-1]
        group1 = cleaned[:-3]
        candidate = f"{group1}-{group2}-{check_digit}"
        
        if validate_cas_checksum(candidate):
            return {
                'is_valid': False,
                'status': 'HCID_DETECTED',
                'suggestion': candidate,
                'original': cleaned,
                'note': f"Detected HCID (hyphenless CAS). Suggested fix: {candidate}"
            }

    # 3. Check for Excel date corruption pattern (e.g., 1950-02-02)
    date_match = re_excel_date.match(cleaned)
    if date_match:
        mm, dd = date_match.groups()
        yy = cleaned[2:4]
        day_digit = str(int(dd))
        candidate = f"{yy}-{mm}-{day_digit}"
        if validate_cas_checksum(candidate):
            return {
                'is_valid': False,
                'status': 'EXCEL_DATE_CORRUPTION',
                'suggestion': candidate,
                'original': cleaned,
                'note': f"Detected Excel date conversion. Suggested fix: {candidate}"
            }

    # 4. Fallback invalid CAS
    return {
        'is_valid': False,
        'status': 'INVALID_FORMAT_OR_CHECKSUM',
        'suggestion': None,
        'original': cleaned,
        'note': "Failed CAS checksum, HCID parsing, or formatting rules."
    }
