# IITR Connect: user guide

IITR Connect is where portal users ask technical questions, share solutions and find reviewed guidance about the instruments they use. It is private: only signed-in portal users can open it, and each equipment group's community is visible only to its members and facility staff.

Open it from your profile menu (**IITR Connect**), from the **Discussions** tab of any equipment page, or at `/connect`.

## Who sees what

- **Equipment Group communities.** You join automatically when you book any instrument in the group, or when you are its Officer-in-Charge (OIC) or Lab Operator (Lab In-charge). External users join the groups they booked.
- **General IITR Discussions.** Open to IIT Roorkee students, faculty and staff. External users do not see it.
- **Unavailable content.** A discussion you cannot open looks the same as one that does not exist. Guessing a link never gives access.

## Asking and answering

1. **Start a Discussion.** Choose the community, type (Question, Technical Issue, Solution, Suggestion, Feature Request, Research, Poll…), category and instrument, then write.
2. **Search first.** While you type the title, similar discussions appear, including accepted answers. Check them before you post.
3. **Write your post.** Add images (paste or drag them in), PDFs, Office or CSV files and code blocks. Type `@` to mention someone in the community. Your draft saves automatically.
4. **Reply.** Reply to the thread or to a specific post. React with *Helpful*, *Insightful* or *Thanks* (not on your own posts).
5. **Follow or bookmark** threads, and **follow** whole communities, to be notified.

## Solved and Resolved

- **Accepting an answer.** On a Question or Technical Issue, the person who asked, or a moderator, can accept the reply that solved it.
- **Resolved.** If you asked, choose **This solved it: mark Resolved**, or **Mark as Resolved** on an answer a moderator accepted. A green **Resolved** badge then shows on the discussion everywhere.
- **Solved.** A moderator-accepted answer without your confirmation shows **Solved**.
- **Verified badges.** Replies from facility staff carry verified badges: Officer-in-Charge, Lab Operator (Lab In-charge), Group Moderator. Community replies are not official instructions. Always follow the instrument's SOP.

## Knowledge Hub

- **Reviewed articles.** Facility staff turn solved discussions into reviewed articles.
- **The guidance label.** Only articles marked **Reviewed institutional guidance** have been approved for the current version. An article edited since its review says so.

## Announcements

- **Who posts them.** Facility staff post announcements. Some are pinned, have a priority or expire, and some ask you to choose **I have read this**.
- **What they never do.** Announcements never change your bookings.

## Notifications

- **Where.** The portal bell and email.
- **Settings.** In **IITR Connect → Notifications** choose immediate email, a daily or weekly digest, or no email. You can also switch individual kinds of notification off.
- **What emails contain.** Only the discussion title and a link, never post text.

## For facility staff (OIC, Lab Operator, moderators)

- **Staff tools** on a thread: pin, known problem, workaround available, outdated guidance, mark issue resolved, lock replies. These never change the instrument's availability or bookings.
- **Moderate** a post: hide or remove it with a reason (the author is told), or restore it.
- **Promote to Knowledge Hub** from a thread. Articles then go through *Send for review* → *Approve* (by someone other than the author) → *Publish*.
- **Announcements:** priority, show-until date, notify only users of one instrument, acknowledgement (you see "N of M acknowledged"), pin.
- **Moderation** page: reports, hidden posts and the audit log for your communities.
- **Analytics** page: unanswered questions, recurring problems, most discussed equipment and useful solutions. Discussion activity is not a measure of downtime.

## For the Portal Admin

- **Moderation → Community Admins:** appoint IITR users who can moderate every community.
- **Moderation → Module settings:** recognition badges (off by default), edit and delete windows, attachment limits, and the waiting period for new accounts.
- **Membership:** the membership sync runs automatically and nightly. An admin dry run or apply is also available through `/api/v1/connect/admin/sync/` or the `connect_sync_communities` command.
