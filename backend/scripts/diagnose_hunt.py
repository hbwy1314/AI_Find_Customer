#!/usr/bin/env python3
"""Diagnose why a Hunt produced fewer leads than expected.

Usage:
    python scripts/diagnose_hunt.py <hunt_id>
    
Example:
    python scripts/diagnose_hunt.py fc33275e-1b9c-4163-bee2-8925746380f4
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

def analyze_hunt(hunt_path: Path) -> None:
    """Analyze a Hunt file and show the filtering pipeline."""
    
    with open(hunt_path, "r", encoding="utf-8") as f:
        hunt = json.load(f)
    
    result = hunt.get("result", {})
    search_results = result.get("search_results", [])
    leads = result.get("leads", [])
    keyword_stats = result.get("keyword_search_stats", {})
    
    print(f"\n{'='*80}")
    print(f"Hunt Diagnosis: {hunt.get('id', 'unknown')}")
    print(f"{'='*80}\n")
    
    print(f"📊 Overall Statistics:")
    print(f"   Search results: {len(search_results)}")
    print(f"   Final leads: {len(leads)}")
    print(f"   Conversion rate: {len(leads)/len(search_results)*100:.1f}%" if search_results else "   Conversion rate: N/A")
    print()
    
    # Analyze search results
    print(f"🔍 Search Results Analysis:")
    has_link = sum(1 for r in search_results if r.get("link"))
    has_maps = sum(1 for r in search_results if r.get("maps_data"))
    has_both = sum(1 for r in search_results if r.get("link") and r.get("maps_data"))
    
    print(f"   With website link: {has_link} ({has_link/len(search_results)*100:.1f}%)" if search_results else "   No results")
    print(f"   With Maps data: {has_maps} ({has_maps/len(search_results)*100:.1f}%)" if search_results else "")
    print(f"   With both: {has_both}")
    print()
    
    # Show sample search results
    print(f"📋 Sample Search Results (first 10):")
    for i, r in enumerate(search_results[:10], 1):
        title = r.get("title", "NO TITLE")
        link = r.get("link", "NO LINK")
        maps = r.get("maps_data", {})
        website = maps.get("website", "") if maps else ""
        
        print(f"\n   {i}. {title}")
        print(f"      Link: {link}")
        if website:
            print(f"      Maps Website: {website}")
        if maps:
            print(f"      Maps Type: {maps.get('type', 'unknown')}")
            print(f"      Maps Address: {maps.get('address', 'none')}")
    print()
    
    # Analyze domains
    print(f"🌐 Domain Analysis:")
    result_domains = set()
    for r in search_results:
        link = r.get("link", "")
        if link:
            domain = urlparse(link).netloc
            if domain:
                result_domains.add(domain)
        
        maps = r.get("maps_data", {})
        if maps and maps.get("website"):
            domain = urlparse(maps["website"]).netloc
            if domain:
                result_domains.add(domain)
    
    lead_domains = set()
    for lead in leads:
        website = lead.get("website", "")
        if website:
            domain = urlparse(website).netloc
            if domain:
                lead_domains.add(domain)
    
    print(f"   Unique domains in search results: {len(result_domains)}")
    print(f"   Unique domains in leads: {len(lead_domains)}")
    print(f"   Domains that became leads: {len(lead_domains & result_domains)}")
    print()
    
    # Analyze lead quality
    print(f"✅ Lead Quality Analysis:")
    for i, lead in enumerate(leads, 1):
        print(f"\n   {i}. {lead.get('company_name', 'NO NAME')}")
        print(f"      Website: {lead.get('website', 'none')}")
        print(f"      Emails: {len(lead.get('emails', []))}")
        print(f"      Phones: {len(lead.get('phone_numbers', []))}")
        print(f"      Fit Score: {lead.get('fit_score', 0):.2f}")
        print(f"      Customer Role: {lead.get('customer_role', 'unknown')}")
        
        # Show fit reasons
        fit_reasons = lead.get("fit_reasons", [])
        if fit_reasons:
            print(f"      Fit Reasons:")
            for reason in fit_reasons[:2]:
                print(f"        - {reason[:100]}")
    print()
    
    # Analyze keyword performance
    print(f"🎯 Keyword Performance:")
    for keyword, stats in keyword_stats.items():
        result_count = stats.get("result_count", 0)
        leads_found = stats.get("leads_found", 0)
        conversion = f"{leads_found/result_count*100:.0f}%" if result_count else "N/A"
        
        print(f"   {keyword}")
        print(f"      Results: {result_count}, Leads: {leads_found}, Conversion: {conversion}")
    print()
    
    # Check for common issues
    print(f"⚠️  Potential Issues:")
    issues = []
    
    if len(leads) < len(search_results) * 0.1:
        issues.append(f"Very low conversion rate ({len(leads)/len(search_results)*100:.1f}%) - possible over-filtering")
    
    no_email_leads = sum(1 for lead in leads if not lead.get("emails"))
    if no_email_leads > len(leads) * 0.5:
        issues.append(f"{no_email_leads}/{len(leads)} leads have no email - contact extraction may be failing")
    
    zero_score_leads = sum(1 for lead in leads if lead.get("fit_score", 0) == 0)
    if zero_score_leads > 0:
        issues.append(f"{zero_score_leads}/{len(leads)} leads have fit_score=0 - scoring may be broken")
    
    unknown_role_leads = sum(1 for lead in leads if lead.get("customer_role") == "unknown")
    if unknown_role_leads > len(leads) * 0.5:
        issues.append(f"{unknown_role_leads}/{len(leads)} leads have unknown customer role")
    
    if not issues:
        print("   No obvious issues detected.")
    else:
        for issue in issues:
            print(f"   - {issue}")
    print()
    
    print(f"{'='*80}\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/diagnose_hunt.py <hunt_id>")
        sys.exit(1)
    
    hunt_id = sys.argv[1]
    
    # Try both local and remote paths
    local_path = Path(f"backend/data/hunts/{hunt_id}.json")
    remote_path = Path(f"/opt/ai-hunter/repo/backend/data/hunts/{hunt_id}.json")
    
    hunt_path = None
    if local_path.exists():
        hunt_path = local_path
    elif remote_path.exists():
        hunt_path = remote_path
    else:
        # Try current directory
        cwd_path = Path(f"data/hunts/{hunt_id}.json")
        if cwd_path.exists():
            hunt_path = cwd_path
    
    if not hunt_path:
        print(f"❌ Hunt file not found: {hunt_id}")
        print(f"   Tried:")
        print(f"   - {local_path.absolute()}")
        print(f"   - {remote_path}")
        print(f"   - {cwd_path.absolute()}")
        sys.exit(1)
    
    analyze_hunt(hunt_path)
