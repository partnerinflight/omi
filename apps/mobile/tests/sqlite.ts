// node:sqlite behind the subset of the expo-sqlite API that src/ uses.
import { DatabaseSync } from 'node:sqlite';
import type { Db } from '../src/db.ts';

export function openDb(path: string): Db {
  const raw = new DatabaseSync(path);
  const q = {
    runAsync: async (sql: string, ...args: any[]) => {
      const r = raw.prepare(sql).run(...args.flat());
      return { lastInsertRowId: Number(r.lastInsertRowid), changes: Number(r.changes) };
    },
    getFirstAsync: async (sql: string, ...args: any[]) => (raw.prepare(sql).get(...args.flat()) ?? null) as any,
    getAllAsync: async (sql: string, ...args: any[]) => raw.prepare(sql).all(...args.flat()) as any,
  };
  return {
    ...q,
    execAsync: async (sql: string) => raw.exec(sql),
    withExclusiveTransactionAsync: async (task) => {
      raw.exec('BEGIN EXCLUSIVE');
      try {
        await task(q as any);
        raw.exec('COMMIT');
      } catch (e) {
        raw.exec('ROLLBACK');
        throw e;
      }
    },
  } as Db;
}
