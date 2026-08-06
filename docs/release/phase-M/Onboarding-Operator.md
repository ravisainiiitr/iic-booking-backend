# Operator Onboarding — Lab Incharge / OIC

## Access

- Login: https://equip.iitr.ac.in  
- Role: Lab Incharge / Officer In Charge / operator permissions  

## Daily workflow

1. Open Lab Operator / Booking Management dashboard.  
2. Confirm equipment status **Operational**.  
3. Process sample lifecycle:  
   - Internal: Accept after Sample Sent  
   - External: **Hold at Office** → **Forward to Lab** → **Accept**  
4. Run analysis on instrument; ensure results land in DSA Active folder  
   `D:\Results\Active\<booking_reference>\` (IIC PXRD example).  
5. Verify results on portal; **Complete** booking.  
6. Handle disruptions (absent, maintenance) via booking actions as needed.  

## Remote Analysis (if enabled)

- User analyzes from completed booking; operator ensures Analysis PC is online/AVAILABLE.  
- If PC stuck BUSY/RESERVED with no session: escalate for `CLEAN_WORKSTATION`.  

## Do / Don't

- Do verify booking reference before dropping files.  
- Do require reason on Hold / Reject.  
- Don't share operator tokens.  
- Don't complete without checking sample status for audit.  
