import catalog from "./generated/club-index.json";
import type { Team } from "./types";

export type Club = Pick<Team, "id" | "name"> & {
  english_name: string;
  aliases: string[];
  crest_url: string;
  country: string;
  city: string;
  description: string;
  official_url: string;
};

export const clubCatalog: readonly Club[] = catalog;

function normalizeClubName(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .trim()
    .toLowerCase()
    .replace(/&amp;|&/g, " and ")
    .replace(/[\p{P}\p{Z}\s_]+/gu, "");
}

const clubsByName = new Map<string, Club>();
for (const club of clubCatalog) {
  for (const name of [club.id, club.name, club.english_name, ...club.aliases]) {
    clubsByName.set(normalizeClubName(name), club);
  }
}

/** Resolve known club names without guessing a badge for an unknown opponent. */
export function findClub(
  nameOrId: string | null | undefined,
): Club | undefined {
  if (!nameOrId) return undefined;
  return clubsByName.get(normalizeClubName(nameOrId));
}
