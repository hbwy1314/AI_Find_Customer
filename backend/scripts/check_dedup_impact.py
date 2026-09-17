"""Check how many candidates would be filtered by global dedup."""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.lead_identity import candidate_identity_keys, normalize_domain
from emailing.store import get_email_store


def check_dedup_impact(hunt_file: str):
    """Check how many seen_urls would be blocked by global registry."""
    with open(hunt_file) as f:
        hunt = json.load(f)
    
    hunt_id = hunt["hunt_id"]
    result = hunt.get("result", {})
    seen_urls = result.get("seen_urls", [])
    
    # Load global registry
    store = get_email_store()
    registry_keys = store.list_lead_registry_keys()
    
    print(f"=== Dedup Impact Analysis ===")
    print(f"Hunt: {hunt_id}")
    print(f"Total seen_urls: {len(seen_urls)}")
    print(f"Global registry size: {len(registry_keys)}")
    
    # Simulate dedup check
    blocked_by_type = {}
    blocked_urls = []
    
    for url_entry in seen_urls:
        if not url_entry.startswith("url:"):
            continue
        
        url = url_entry[4:]  # Remove "url:" prefix
        
        # Generate identity keys for this candidate
        domain = normalize_domain(urlparse(url).netloc)
        
        # Simulate what candidate_identity_keys would generate
        # For URLs without full candidate data, we can only check domain
        candidate_keys = []
        if domain:
            candidate_keys.append(f"domain:{domain}")
        
        # Check if any key exists in registry
        for key in candidate_keys:
            if key in registry_keys:
                existing_hunt = store.get_hunt_id_for_key(key)
                if existing_hunt and existing_hunt != hunt_id:
                    key_type = key.split(":")[0]
                    blocked_by_type[key_type] = blocked_by_type.get(key_type, 0) + 1
                    blocked_urls.append((url, key, existing_hunt))
                    break
    
    print(f"\n=== Dedup Blocking Results ===")
    print(f"URLs blocked by global dedup: {len(blocked_urls)}")
    print(f"URLs that would pass dedup: {len([u for u in seen_urls if u.startswith('url:')]) - len(blocked_urls)}")
    
    if blocked_by_type:
        print(f"\nBlocked by identity type:")
        for key_type, count in sorted(blocked_by_type.items(), key=lambda x: x[1], reverse=True):
            print(f"  {key_type}: {count}")
    
    if blocked_urls:
        print(f"\n=== Sample Blocked URLs (first 20) ===")
        for url, key, existing_hunt in blocked_urls[:20]:
            print(f"  {url}")
            print(f"    Blocked by: {key}")
            print(f"    From hunt: {existing_hunt[:8]}...")
    
    # Calculate filtering rate
    total_urls = len([u for u in seen_urls if u.startswith("url:")])
    if total_urls > 0:
        dedup_rate = len(blocked_urls) / total_urls * 100
        print(f"\n=== Summary ===")
        print(f"Global dedup filtering rate: {dedup_rate:.1f}%")
        
        if dedup_rate > 50:
            print("⚠️  CRITICAL: Over 50% filtered by global dedup")
            print("This explains why so few leads are found!")
        elif dedup_rate > 30:
            print("⚠️  WARNING: High dedup rate")
        else:
            print("✓ Reasonable dedup rate")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python check_dedup_impact.py <hunt_file>")
        sys.exit(1)
    
    check_dedup_impact(sys.argv[1])
