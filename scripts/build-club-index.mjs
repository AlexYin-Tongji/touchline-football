import { readFile, mkdir, writeFile } from 'node:fs/promises';
const root = new URL('../', import.meta.url);
const catalog = JSON.parse(await readFile(new URL('public/assets/clubs/catalog.json', root), 'utf8'));
const fields = ['id', 'name', 'english_name', 'aliases', 'crest_url', 'country', 'city', 'description', 'official_url'];
const index = catalog.map(club => Object.fromEntries(fields.map(field => [field, club[field] ?? (field === 'aliases' ? [] : '')])));
await mkdir(new URL('src/generated/', root), { recursive: true });
await writeFile(new URL('src/generated/club-index.json', root), JSON.stringify(index));
