// node:sqlite behind the subset of the expo-sqlite API that src/ uses. Like expo-sqlite,
// withExclusiveTransactionAsync opens its own connection and runs a plain BEGIN, so two
// overlapping write transactions hit SQLITE_BUSY ("database is locked") exactly as on the phone.
import { DatabaseSync } from 'node:sqlite';
import type { Db } from '../src/db.ts';

function queries(raw: DatabaseSync) {
  return {
    runAsync: async (sql: string, ...args: any[]) => {
      const r = raw.prepare(sql).run(...args.flat());
      return { lastInsertRowId: Number(r.lastInsertRowid), changes: Number(r.changes) };
    },
    getFirstAsync: async (sql: string, ...args: any[]) => (raw.prepare(sql).get(...args.flat()) ?? null) as any,
    getAllAsync: async (sql: string, ...args: any[]) => raw.prepare(sql).all(...args.flat()) as any,
  };
}

export function openDb(path: string): Db {
  const raw = new DatabaseSync(path);
  return {
    ...queries(raw),
    execAsync: async (sql: string) => raw.exec(sql),
    withExclusiveTransactionAsync: async (task) => {
      const conn = new DatabaseSync(path);
      try {
        conn.exec('BEGIN');
        try {
          await task(queries(conn) as any);
          conn.exec('COMMIT');
        } catch (e) {
          conn.exec('ROLLBACK');
          throw e;
        }
      } finally {
        conn.close();
      }
    },
    close: () => raw.close(),
  } as Db;
}
