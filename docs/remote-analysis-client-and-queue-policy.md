# Remote Analysis: client requirements and queue policy

What a user needs to run a Remote Analysis session, how the queue orders and informs people, and what happens
when something goes wrong. The frontend's `src/lib/analysisDevicePolicy.ts` implements the device rules; the
backend's `iic_booking/remote_analysis/services/queue_insight.py` and `services/checkin.py` implement the queue.

## How users connect

There is one connection method: the portal's in-browser remote desktop, opened from **Join session** on the
Analysis Workspace page. Nothing is installed on the user's computer.

- No RDP (`.rdp`) file. Analysis PCs sit on the equipment LAN behind the portal's reverse tunnel, so a desktop RDP
  client cannot reach them, and the portal never gives out the PC's Windows password.
- No app. The Android app is for staff and does not open analysis sessions.

## Minimum requirements

| | Supported | Warned | Blocked (alternatives shown) |
|---|---|---|---|
| Device | Laptop or desktop: Windows 10+, macOS 12+, Linux, ChromeOS | Tablet (attach a keyboard and mouse) | Phone |
| Browser | Chrome or Edge 109+, Firefox 115+, Safari 16+ | Other browsers (untested); on a tablet, anything but Chrome or Safari | Older versions of the supported browsers; Internet Explorer |
| Window | 1024 × 600 or larger | Smaller window | — |
| Browser settings | Cookies and WebSockets allowed | — | Either one off |
| Network | ≤ 150 ms delay to the portal, about 2 Mbps | 150–300 ms (usable), over 300 ms (laggy) | Offline, or the portal cannot be reached |

On a blocked device the page explains why and offers alternatives: use a laptop or desktop (campus lab PCs meet
the requirements), keep watching the queue on the phone and be notified when the PC is ready, or ask the Lab
Operator from the booking details page. Phones are still allowed to watch the queue and receive notices.

These rules follow what the large virtual-desktop services publish:

- Microsoft's Remote Desktop web client (Azure Virtual Desktop) does not support mobile browsers; phones use the app.
  <https://learn.microsoft.com/azure/virtual-desktop/users/connect-web>
- Amazon WorkSpaces web access supports the latest versions of Chrome, Edge, Firefox and Safari on Windows, macOS
  and Linux, and only Chrome and Safari on tablets.
  <https://docs.aws.amazon.com/workspaces/latest/userguide/amazon-workspaces-web-access.html>

## Queue

- **Order.** One first-come, first-served line (priority, then time joined). Requests only compete for PCs that
  have their software, so position, wait and the "someone is waiting" check count only requests that could use the
  same PCs.
- **What the user sees** (Queue-it's waiting-room practice: place in line, people ahead, estimated wait, when the
  status was last updated, and a notice when it is your turn;
  <https://queue-it.com/blog/custom-waiting-room-experience/>):
  people ahead, estimated wait and start time, the reason for the wait, when the page last checked, and that they
  may leave the page. The estimate simulates the compatible PCs: each frees when its current session ends, and
  everyone ahead takes the next free PC for one average session. With no compatible PC online there is no estimate,
  and the page says so instead of showing a made-up number.
- **Reasons:** others asked first; all compatible PCs busy; the PC is not connected; the PC is under maintenance;
  no PC has the software.
- **A queued request keeps its place** until its booking's access window ends (it used to expire after 30 minutes).

## When a PC is free: the hold

- The PC is held for the next person for the equipment's check-in time (default 10 minutes, at most 120).
- They are told on every channel: the page (toast, chime, flashing tab title, a browser notification if they allowed
  it), the portal's notification bell, email, and WhatsApp (template `iitr_remote_analysis_ready`, sent once Meta
  approves it; until then the attempt is recorded as skipped).
- **Join session** starts the desktop. **Give it to the next person** releases the hold and leaves the queue.
- A missed hold moves the request to the end of the queue. After three missed holds (the equipment's limit) the
  request is closed and the user is told they can ask again; it no longer loops for the whole access window.
- A booking that completes no longer reserves a PC by itself; the PC is held only when the user asks for analysis.

## During the session

- **Time warnings** at 10, 5, 2 and 1 minutes in the bar above the desktop; at 5 and 1 minutes also a toast, a chime
  and a browser notification if the tab is in the background. An **Add N min** button appears when nobody is waiting
  for that PC.
- **Idle timeout.** The page sends a keepalive every minute while the user is working, because the agent on the PC
  runs as a Windows service and cannot see the remote user's input. Two minutes before the timeout the page asks
  "Are you still working?" (Citrix shows a similar warning one minute before an idle disconnect;
  <https://docs.netscaler.com/en-us/citrix-gateway/12-1/vpn-user-config/configure-plugin-connections/ng-plugin-config-timout-settings-overview-con>).
- **Network drop.** The page says the connection is lost, that the session keeps running on the PC, and reconnects
  by itself when the browser is back online. **Reconnect** is always available.
- **Time up.** The page checks with the server before closing (an extension made elsewhere wins), then shows the
  results being saved. If the server ends the session (idle timeout, ended in another tab, staff action, lost agent),
  the page notices within 20 seconds and moves to the finish screen.
