# Routing Engine Enhancements

## 1. Tanker Travel Time Adjustment

Google Maps gives driving times based on a regular car, which tends to run shorter than what a fully loaded tanker trailer actually takes on the road. Based on real-world observation, we add a flat 20% on top of every travel time estimate the system produces. This applies whether the time came from Google Maps or the built-in distance fallback — everything gets the same adjustment. The 20% figure is stored in one place in the code, so if we ever want to fine-tune it based on more data, it's a quick change.

---

## 2. Smarter Load Assignment (2-Opt Improvement)

The original dispatch logic works by assigning loads one at a time in order — once a load is given to a driver, that decision sticks, even if a better overall arrangement turns up later. To fix this, after all the initial assignments are made, the system does a second pass where it looks at every pair of drivers and asks: would swapping one of their loads produce a better result? It also checks whether moving a load from a busy driver over to a lighter one would help. Any change that results in less empty driving and more productive miles gets accepted. This keeps repeating until no more improvements can be found, giving the final schedule a much better chance of being the most efficient arrangement rather than just the first one that worked.

---

## 3. Second Chance for Unassigned Loads

When a load can't be fit into any driver's day during the first pass, the system marks it unassigned. But that decision is made before the improvement step above has a chance to free up space by reshuffling other loads. So before calling anything truly unassigned, the system takes another look at loads that failed only because there wasn't enough time in the day — not because of a rule violation like a driver not being certified for that site. It tries those loads again, starting with the drivers who have the lightest workload and the most shift time remaining. Anything that finds a home gets added to the schedule; only loads that still have no viable option after this second attempt are left as unassigned.
