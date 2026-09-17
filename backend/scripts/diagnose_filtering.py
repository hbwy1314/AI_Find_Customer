"""Diagnose why candidates are filtered in lead extraction."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.lead_identity import candidate_identity_keys


def diagnose_hunt(hunt_file: str):
    """Analyze a hunt file to understand filtering behavior."""
    with open(hunt_file) as f:
        hunt = json.load(f)
    
    hunt_id = hunt["hunt_id"]
    result = hunt.get("result", {})
    seen_urls = result.get("seen_urls", [])
    leads = result.get("leads", [])
    keyword_stats = result.get("keyword_search_stats", {})
    
    print(f"=== Hunt {hunt_id} ===")
    print(f"Status: {hunt['status']}")
    print(f"Stage: {hunt.get('current_stage')}")
    print(f"Round: {hunt.get('hunt_round')}")
    print(f"\n=== Overview ===")
    print(f"Total seen_urls: {len(seen_urls)}")
    print(f"Total leads: {len(leads)}")
    print(f"Keywords: {len(keyword_stats)}")
    
    # Calculate search results vs leads
    total_search_results = sum(s.get("result_count", 0) for s in keyword_stats.values())
    total_leads_from_keywords = sum(s.get("leads_found", 0) for s in keyword_stats.values())
    
    print(f"\n=== Search → Leads Funnel ===")
    print(f"Total search results: {total_search_results}")
    print(f"Total leads extracted: {total_leads_from_keywords}")
    print(f"Conversion rate: {total_leads_from_keywords / max(total_search_results, 1) * 100:.1f}%")
    
    # Keyword performance
    print(f"\n=== Keywords with 0 Leads (but had results) ===")
    zero_lead_keywords = [
        (kw, stats) for kw, stats in keyword_stats.items()
        if stats.get("result_count", 0) > 0 and stats.get("leads_found", 0) == 0
    ]
    zero_lead_keywords.sort(key=lambda x: x[1]["result_count"], reverse=True)
    
    for kw, stats in zero_lead_keywords[:10]:
        print(f"  {kw}: {stats['result_count']} results → 0 leads")
    
    # Analyze global dedup impact
    print(f"\n=== Identity Key Analysis ===")
    print("Checking what identity keys the leads have...")
    
    identity_key_types = {}
    for lead in leads:
        keys = candidate_identity_keys({
            "url": lead.get("website", ""),
            "title": lead.get("company_name", ""),
            "maps_data": lead.get("maps_data", {}),
            "emails": lead.get("emails", []),
            "phones": lead.get("phones", []),
        })
        
        for key in keys:
            key_type = key.split(":")[0]
            identity_key_types[key_type] = identity_key_types.get(key_type, 0) + 1
    
    print("Identity key types in actual leads:")
    for key_type, count in sorted(identity_key_types.items()):
        print(f"  {key_type}: {count}")
    
    # Check if we can detect company name collisions
    print(f"\n=== Potential Company Name Collisions ===")
    company_names = {}
    for lead in leads:
        name = lead.get("company_name", "").lower().strip()
        if name:
            company_names[name] = company_names.get(name, 0) + 1
    
    duplicates = {name: count for name, count in company_names.items() if count > 1}
    if duplicates:
        print("Company names appearing multiple times:")
        for name, count in duplicates.items():
            print(f"  {name}: {count}")
    else:
        print("No duplicate company names in final leads")
    
    # Sample a few seen_urls to understand what was searched
    print(f"\n=== Sample Seen URLs ===")
    url_samples = [u for u in seen_urls if u.startswith("url:")][:10]
    for url in url_samples:
        print(f"  {url}")
    
    print(f"\n=== Recommendation ===")
    if total_search_results > 50 and total_leads_from_keywords < 10:
        print("⚠️  HIGH FILTERING DETECTED")
        print("Likely causes:")
        print("  1. Global dedup registry blocking too many candidates")
        print("  2. QuickGate rejecting valid B2B companies")
        print("  3. Company name collisions (same name, different regions)")
        print("\nSuggested fixes:")
        print("  - Make company name a WEAK identity key (require + country/region)")
        print("  - Add QuickGate pass/reject logging")
        print("  - Add dedup stats to hunt result")
    elif total_search_results < 20:
        print("⚠️  LOW SEARCH RESULTS")
        print("Consider improving keyword strategy")
    else:
        print("✓ Reasonable conversion rate")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python diagnose_filtering.py <hunt_file>")
        sys.exit(1)
    
    diagnose_hunt(sys.argv[1])
