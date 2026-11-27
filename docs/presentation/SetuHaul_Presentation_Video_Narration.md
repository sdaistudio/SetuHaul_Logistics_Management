# SetuHaul – End-to-end walkthrough: voice-over script

Order follows the team presentation (deck slide number in brackets). About 8 minutes 15 seconds.

## 1. Cover (slide 1)

This is SetuHaul. It's nine fifteen on the fourth of August at Jaipur D C. Dock D three has just broken down. Thirteen of the twenty trucks due today are running late, and in the next half hour up to thirty five drivers will message, all asking the same question: where do I go now? Our answer: a late driver sends one WhatsApp message, in his own language, and gets a dock slot that really works, booked safely. We are Varadhan, Surinder, Hamsa, Shantanu and Angelin.

## 2. Context and problem (slide 2)

SetuHaul moves up to three hundred and sixty truckloads a day through six facilities and thirty two dock doors. On paper there is enough capacity. But in a disruption every driver chases the same few compatible doors, and with no G P S, every arrival time comes from the driver. That morning, a broken dock still showed open slots, and a refrigerated booking had been impossible for three days. So the problem is this: answer the late driver in his language, rebook a slot that really works in one safe step, and send only judgement calls to a person.

## 3. Scope and approach (slide 3)

M V P one answers the driver. M V P two reaches him first, with G P S. Our approach fits in one sentence: the model reads, code decides, the database refuses, and a person decides what code must not. We audited the data first, wrote down our decisions, built, replayed the real day, proved it under pressure, and shipped it on A W S.

## 4. Data insights (slide 4)

Before writing any engine code, we treated the data as evidence, not truth. We ran thirty six S Q L checks on the snapshot. Five were critical and fifteen high. Broken docks look open, because dock events never reach slot status. A reefer booking was dead for three days. And one slot held two confirmed bookings for three days, which the supplied unique indexes did not stop. So every finding became a control. We kept all eighteen supplied tables untouched, and added twelve tables and three database triggers that refuse overlaps, wrong doors and overweight loads, even if our own code has a bug.

## 5. Decision register and flow (slide 8)

The brief asked thirteen hard questions. We answered all of them, added eight of our own, and every answer carries evidence. Showing options reserves nothing. The first valid commit wins, but scarce doors are protected for critical loads and hard deadlines, and claims of urgency change nothing. With no feasible slot, we never invent one. Here is the flow of one message. Rules read it, and the L L M helps only when rules cannot. Safety words stop everything and page a manager. Otherwise we find the trip and its E T A, re-plan the facility, check fifteen rules per slot, and offer two or three options. The commit runs under a lock, and a driver who loses a race gets fresh options, never an error. The model only reads words. Every branch after that is code.

## 6. Five use cases (slide 5)

We rewound the client's database to eight twenty that morning and replayed the day, message by message: thirteen real driver conversations. Ten were settled without a person, the median answer took one minute, and no slot was ever promised twice. The three that reached a person were exactly the ones the brief says must. Ramesh reported an accident in Hinglish, and the shift manager was paged at once. Mohammed's reefer had no feasible slot, so a coordinator got his case with what-ifs. And a data conflict went for approval.

## 7. Use case spotlight: C03 (slide 6)

Let's watch one case. At nine nineteen, Kailash writes: I am late by one hour. The engine records his new E T A, re-plans Jaipur, and offers three slots he can make. Nothing is booked until he chooses. He picks D four at twelve forty, but another driver booked it moments earlier. There is no error and no double booking. He gets fresh options, picks D one at eleven thirty, and it is confirmed. It cost him one extra message.

## 8. Architecture (slide 7)

The architecture has five layers. Channels: WhatsApp, voice notes and the coordinator console. The edge: the webhook, sender checks, de-duplication, STOP, and an outbox with retries. Understanding: language only, with rules in seven languages, an L L M fallback, and a reviewed reply catalogue. Decide: plain, deterministic code. And data: S Q Lite with triggers, Redis sessions and nightly backups. Only layer four decides, only one file writes a booking, and layer five refuses anything the code gets wrong.

## 9. Tool calling, code level (slide 9)

Here, the model never sees the tool list. Each tool is a plain Python function, called by the workflow in a fixed order. The heart of it is capacity dot commit, the only function that writes a booking. It takes the write lock before reading, returns the first answer for a retried message, re-runs all fifteen checks, and returns option gone if the slot was taken. Anything that slips past is refused by the database triggers. That is five layers of defence, and none of them depends on the model.

## 10. AWS deployment (slide 10)

Deployment is deliberately small. One E C two server runs three containers. Caddy gives automatic H T T P S and exposes only the webhook, the media path and a health check. The fast A P I app runs a single worker, because S Q Lite has one writer. And Redis holds sessions. Twilio connects WhatsApp, secrets live in S S M, backups go to S three every night, and a CloudWatch alarm recovers the server. It runs today for about thirteen U S dollars a month, and moves to Mumbai for the pilot.

## 11. Evals and guardrails (slide 12)

One hundred and seventy five automated tests run in about twenty seconds, plus four harnesses: a replay of the real morning; a race proof where ten drivers hit one slot at once, fifty times, with exactly one winner every time; a language replay in six more languages; and a determinism check. The test that matters most is designed to fail. With the database guards switched off, all ten drivers book the same slot. And every guardrail is code you can open: an output guard on every reply, validated L L M output that can never clear a safety flag, urgency ignored, and no phone numbers or names sent to a model.

## 12. Models and MVP1 cost (slide 11)

Because models sit only at the edges, costs stay low. At the brief's scale, about eight hundred and ninety exception chats a month across six facilities, M V P one costs about five thousand six hundred and seventy rupees a month, roughly six rupees per exception. Voice adds about two thousand and eighty rupees.

## 13. MVP2 proposition (slide 13)

M V P two knows first. Every trip gets a red, amber or green flag against its slot. A series of eight WhatsApp messages, from a trip brief the day before to arrival paperwork, reaches the driver before he has to call us. The engine stays the same. Only the E T A source changes: FASTag toll crossings through ULIP, the carrier's own G P S, and devices only where neither exists.

## 14. Cost comparison (slide 14)

Because M V P two messages every load, about ten thousand eight hundred a month, it costs about fifty five thousand nine hundred rupees a month, or about five rupees a load. The biggest line is WhatsApp, since Meta now charges every utility message. Twilio's fee alone is about twenty three thousand rupees. Connecting directly to Meta's A P I brings M V P two down to about thirty two thousand five hundred. Fitting our own G P S devices would add two to six lakh rupees a month, which is why we start with FASTag and carrier data.

## 15. Next steps (slide 15)

Back to nine fifteen on the fourth of August. With SetuHaul, each driver gets an answer in about a minute, in his own language, with a slot his truck can really make. Our next steps: pilot M V P one at Jaipur on real WhatsApp, prepare M V P two with ULIP access and carrier G P S, and agree the client sign-offs. The model reads. Code decides. The database refuses. And a person decides what code must not. Thank you.
