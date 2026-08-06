# Department Onboarding — Equipment Booking Portal v2.5.0

## Purpose

Bring a department onto https://equip.iitr.ac.in with booking, optional Department Sync Agent (DSA), and optional Remote Analysis.

## Prerequisites

- Department record and administrators in portal  
- Equipment catalog entries (Operational status when bookable)  
- Charge profiles for internal / external user types  
- Lab Incharge / OIC accounts with RBAC  
- Network path for results share (if DSA)  

## Steps

1. **Admin setup** — Create department, roles, charge profiles, equipment.  
2. **Operator accounts** — Lab Incharge + backup operator.  
3. **Pilot equipment** — One instrument end-to-end before fleet.  
4. **DSA (optional)** — Install Windows service; enroll; assign sync profile; set watch path; verify heartbeat.  
5. **Remote Analysis (optional)** — Install RAA; register workstation; map software; complete one Analyze cycle.  
6. **Training** — Operator + faculty walkthrough (see sibling onboarding guides).  
7. **Go-live** — Announce booking URL, support contact, sample shipping rules (external).  

## Acceptance

- [ ] Test booking completes with invoice  
- [ ] DSA online (if used) and one result uploaded  
- [ ] RA AVAILABLE and one session (if used)  
- [ ] Department admin can see equipment and reports  
