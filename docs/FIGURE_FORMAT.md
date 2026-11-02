# Figure data layout

What PortalSync's figure editor reads from and writes to a Skylanders figure, and how sure we are of each field.

The offsets come from public sources:

- [Runes](https://github.com/NefariousTechSupport/Runes): `Docs/SkylanderFormat.md` and `source/PortalTag.hpp`.
- [Dolphin](https://github.com/dolphin-emu/dolphin): `Source/Core/Core/IOS/USB/Emulated/Skylanders/SkylanderFigure.cpp`.

Each field was then checked against the 45 dumps in `dumps` (checked 2026-10-27). `verify_layout.py` repeats the checks and must pass before any field becomes editable.

Runes has no license file, so its code and name tables are not copied into PortalSync. Only the facts about offsets are used.

## Status levels

- **Verified**: the dumps prove the field, for example a value that changes the way it should between two saves of one figure, or a value that matches what is known about that figure. The editor may let you edit it.
- **Documented**: Runes (and Dolphin where noted) describe the field, but none of your figures holds data that proves it. The editor shows it read-only, marked "unverified".

## Basics

A figure holds 64 blocks of 16 bytes. Every 4th block (3, 7, 11, …) is a sector trailer that holds no data. Blocks 8-63 are encrypted. `crypt()` in PortalSync decrypts them.

A Skylander stores its save twice, so the previous save stays as a fallback:

- **Main area**: at 0x080 or 0x240. The newer copy has a counter at +0x09 that is one higher, wrapping at 255.
- **Second area**: at 0x110 or 0x2D0. Its counter is at +0x02.

The offsets below are *struct* offsets, as Runes numbers them, with the sector trailers skipped. To turn one into a raw offset in the decrypted image:

- **Main area (struct 0x00-0x6F)**: blocks 0, 1, 2, 4, 5, 6 and 8 from the area start. In code: `raw = area + (blk + blk // 3) * 16 + off`.
- **Second area (struct 0x70-0xAF)**: blocks 0, 1, 3 and 4 from the area start.

Examples:

- Hero points at struct 0x4A are raw `area + 0x5A`, the same offset Dolphin uses.
- 2013 XP at struct 0x78 is raw `second + 0x08`, the same offset PortalSync uses.

## Header (blocks 0-1, never encrypted)

| Offset | Size | Field | Status |
|---|---|---|---|
| 0x00 | 4 | UID (serial) | Verified (file names) |
| 0x10 | 2 | Character id | Verified (`figure_names.json`) |
| 0x13 | 1 | Error byte, must be 0 or the game rejects the toy | Documented |
| 0x14 | 8 | Trading card id, from which the web code is derived (base 29, `23456789BCDFGHJKLMNPQRSTVWXYZ`) | Documented |
| 0x1C | 2 | Variant: deco id (bits 0-7), LightCore (9), in-game variant (10), reposed (11), year code (12-15) | Verified (`figure_names.json`) |
| 0x1E | 2 | CRC16-CCITT of bytes 0x00-0x1D | Verified (PortalSync) |

## Skylander: main area

| Struct | Size | Field | Status | Evidence |
|---|---|---|---|---|
| 0x00 | 3 | XP, first counter (cap 33000) | Verified | PortalSync; Jawbreaker and Bumble Blast are at 33000 |
| 0x03 | 2 | Gold | Verified | PortalSync |
| 0x05 | 4 | Play time, seconds | Verified | Rises between saves of each figure. One exception, see Open points |
| 0x09 | 1 | Area counter | Verified | PortalSync |
| 0x0A-0x0F | 6 | Checksums types 3, 2, 1 | Verified | PortalSync `seal()` |
| 0x10 | 3 | Flags1: upgrades bits 0-9, element collection bits 10-11 | Verified (upgrades) | See Upgrades |
| 0x13 | 1 | Platforms used, 2011 set: Wii, X360, PS3, PC, 3DS | Verified | Zook = Wii + PS3, the Dolphin and RPCS3 user |
| 0x14 | 2 | Hat, Spyro's Adventure set | Documented | All zero on your figures |
| 0x16 | 1 | Region count id | Documented | 1 on all figures, as documented |
| 0x17 | 1 | Platforms used, 2013 set: Wii U/Android, XOne, PS4, iOS, -, -, Switch | Verified | Wii U bit on all Cemu-played figures; PS4 bit on the shadPS4 figure |
| 0x18 | 8 | Owner id (SSA/Giants) | Documented | |
| 0x20 | 32 | Nickname, UTF-16 (struct 0x20-0x3F) | Verified | PortalSync |
| 0x40 | 6 | Last placed: minute, hour, day, month, year (u16) | Verified | Matches every "before restore" file's date; Dolphin agrees |
| 0x46 | 4 | Heroic challenges done, Spyro's Adventure | Documented | All zero |
| 0x4A | 2 | Hero points (max 999 in SSA) | Documented | All zero; Runes and Dolphin agree on the offset |
| 0x4C | 3 | Build date of the last game that wrote it (YY, MM, DD) | Verified | 14-08-25 Trap Team, 13-09-06 Swap Force, 12-08-08 Giants, 16-08-08 Imaginators |
| 0x4F | 1 | Owner changes | Verified | Blast Zone (Top) goes 1 to 2 between saves |
| 0x50 | 6 | Last reset (or first use) date | Verified | Same layout as last placed, stays fixed between saves |
| 0x56 | 10 | Event and console data | Documented | |
| 0x60 | 15 | Usage/owner info | Documented | Unknown even to Runes |

## Skylander: second area

| Struct | Size | Field | Status | Evidence |
|---|---|---|---|---|
| 0x70 | 2 | Checksum type 6 | Verified | PortalSync `b_checksum()` |
| 0x72 | 1 | Area counter | Verified | PortalSync |
| 0x73 | 2 | XP, second counter (cap 63500) | Verified | Even Trigger Happy (an SSA figure) is capped at 63500, not 65535 |
| 0x75 | 1 | Hat, Giants set | Documented | All zero |
| 0x76 | 2 | Flags2: upgrades bits 0-3, accolades, element collection | Verified (upgrades) | |
| 0x78 | 4 | XP, third counter (cap 101000) | Verified | Bumble Blast's original copy holds 101001, so the game can go 1 over |
| 0x7C | 1 | Hat, Swap Force / Trap Team set (ids 97-255) | Verified | Bumble Blast (Jolly) wears 153, the Holiday Bumble Blast hat that variant comes with |
| 0x7D | 1 | Trinket | Documented | Values 2 and 23 are valid ids, but can't be proven |
| 0x7E | 1 | Hat, SuperChargers set (add 256) | Documented | Trigger Happy: 23, giving 279 (Kaos Crown) |
| 0x80 | 4 | Battlegrounds flags | Documented | |
| 0x84 | 3 | Heroic challenges done, Giants | Documented | All zero |
| 0x87 | 9 | Giants quests (packed counters) | Documented | Non-zero on Zook and Trigger Happy; the meaning can't be proven |
| 0x97 | 9 | Swap Force quests (packed counters) | Documented | Same |

### Upgrades (Verified)

The 14 upgrade bits combine both flag fields: `((Flags2 & 0xF) << 10) | (Flags1 & 0x3FF)`.

| Bit | Meaning |
|---|---|
| 0 | A path has been chosen |
| 1 | Which path: 0 = top, 1 = bottom |
| 2-5 | Base upgrades 1-4 |
| 6-8 | Upgrades 1-3 on the chosen path |
| 9 | Soul Gem |
| 10 | Wow Pow (Sky-Chi for Senseis) |
| 11-13 | Upgrades 1-3 on the other path, kept when switching |

Evidence from the dumps:

- Between two saves, Head Rush goes from path upgrade `001` to `111` plus the Soul Gem.
- Blast Zone (Top) goes from `000` to `011`.
- No figure has path upgrades without a chosen path.
- Golden Queen, a Sensei, has bit 10 set.
- On a Swapper half, basic upgrade boxes 1-3 (bits 2-4) are the three basic upgrades the Skylanders Wiki lists, in its order; box 4 (bit 5) has no wiki name. The user's Hoot Loop (Bottom) has only bit 2 set, and the game's Powers screen shows only Portable Hole, the wiki's first, as bought (2026-10-30). The same screen lists that half's basic upgrades in the wiki's order (Portable Hole, Temporal Whack 640 gold, Time Sink 800 gold), with no fourth one, and 4 locked path upgrades: 2 per path. SuperChargers shows the same 3 plus the half's free main power (Loop the Loop, which the wiki lists as a power and which takes no box). Wash Buckler (Top) has all 4 boxes set while SuperChargers shows only its main power and 3 basic upgrades, all bought: box 4 is no fourth upgrade to buy. What box 4 means is still unknown. A Swapper half has 2 path upgrades (bits 6-7).
- Bit 1 = 0 is the path the Skylanders Wiki lists first: the user's Trigger Happy (427dbe01) has bit 1 = 0 and is on Golden Frenzy, the wiki's path 1 (confirmed by the user, 2026-10-28).

### Levels

XP is the sum of the three counters. Each counter fills before the next one starts.

PortalSync's `LEVELS` table (level 20 = 197500 = 33000 + 63500 + 101000) is correct. Runes lists 115735 to 199535 for levels 16-20, but 199535 is out of reach once the second counter is capped at 63500. Jawbreaker and Bumble Blast have all three counters full and count as level 20.

When writing XP, cap the counters at 33000, 63500 and 101000.

## Trap (ids 210-220, Verified)

| Struct | Size | Field | Evidence |
|---|---|---|---|
| 0x01 | 1 | Number of villains stored | Air Jughead 3, Earth Orb 2 |
| 0x0A | 2 | Type 3 checksum over 0x110 struct bytes from 0x40 (all villain slots) | Matches all 8 trap dumps |
| 0x10 + k*0x30 | 1 | Villain id, slot k (k = 0 is the villain in the trap now) | Ids match each trap's element |
| +1 / +2 / +3 | 1 each | Evolved, villain hat, villain trinket | Evolved byte = 1 on Air Jughead's second villain |
| +4 | 28 | Villain nickname, UTF-16 | All empty |

Slot k starts at struct 0x10 for k = 0, then at 0x40, 0x70, 0xA0, 0xD0 and 0x100. A trap's checksum type 3 differs from a Skylander's, so editing a trap needs its own `seal()`.

## Vehicle (SuperChargers, ids 3200-3299)

| Struct | Size | Field | Status | Evidence |
|---|---|---|---|---|
| 0x00 | 3 | Vehicle XP (SuperChargers Racing) | Documented | Burn-Cycle 1892, unchanged |
| 0x05 | 4 | Play time | Verified | Rises between saves |
| 0x10 | 3 | Shield levels (bits 0-4) and weapon levels (bits 5-9) | Documented | All zero |
| 0x18-0x1B | 1 each | Decoration, topper, neon, horn shout | Documented | All zero |
| 0x3E | 2 | Mods: performance (bits 0-3), specialty (4-7), horn (8-11) | Documented | All zero |
| 0x40 | 6 | Last placed | Verified | |
| second 0x78 | 2 | Gearbits (cap 33000) | Verified | The older copy holds 8565 and the newer copy 8620 |

Vehicles use the same checksums as a Skylander. PortalSync's `seal()` fits them: all three area checksums match on Burn-Cycle.

## Creation Crystal / Imaginators (Documented)

Runes says its own notes may be wrong here. Your only crystal, Air Creation Crystal (Angel), is blank, so nothing can be checked. Every crystal field stays read-only:

- money at 0x03
- play time at 0x05
- element at 0x19
- nickname at 0x20-0x3F, in another encoding
- creation data at second-area 0x7C-0xC0

## Open points

- **Trigger Happy (427dbe01) play time** reads 2,661,386,624 s, and grows by 256 per save. The field looks shifted on this figure only. Show it as is and never write it.
- **Hats:** with no license on Runes' hat table, the editor needs its own list of hat names, built from the game ids. Only the ids from 97 up (the Swap Force and Trap Team set, the verified slot) are candidates for editing.
- **When writing a hat**, the Swap Force and later games read the newest slot first and wipe the others. Write one slot and clear the others, as Runes does.
