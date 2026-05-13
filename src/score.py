import re

def extract_abuser_score(score_str):
    if not score_str:
        return 0.0
    match = re.search(r"([\d\.]+)", str(score_str))
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0
    return 0.0

def calculate_risk_score(data):
    if not data:
        return 0
    
    asn_score = extract_abuser_score(data.get("asn", {}).get("abuser_score", "0"))
    company_score = extract_abuser_score(data.get("company", {}).get("abuser_score", "0"))
    part1 = ((asn_score + company_score) / 2.0) * 5.0
    
    bool_fields = ["is_crawler", "is_proxy", "is_vpn", "is_tor", "is_abuser"]
    count_true = sum(1 for field in bool_fields if data.get(field) is True)
    part2 = count_true * 0.15
    
    part3 = 1.0 if data.get("is_bogon") is True else 0.0
    
    final_score = (part1 + part2 + part3) * 100.0
    return int(round(final_score))
