declare module 'node:test' {
  export function describe(name: string, fn: () => void | Promise<void>): void
  export function it(name: string, fn: () => void | Promise<void>): void
  export function test(name: string, fn: () => void | Promise<void>): void
}

declare module 'node:assert/strict' {
  const assert: {
    equal<T>(actual: unknown, expected: T, message?: string): asserts actual is T
    deepEqual<T>(actual: unknown, expected: T, message?: string): asserts actual is T
    ok(value: unknown, message?: string): asserts value
    match(value: string, regexp: RegExp, message?: string): void
    doesNotMatch(value: string, regexp: RegExp, message?: string): void
  }
  export default assert
}

declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf8'): string
  export function readdirSync(
    path: string,
    options: { withFileTypes: true }
  ): Array<{ name: string; isDirectory(): boolean; isFile(): boolean }>
}

declare module 'node:path' {
  export function dirname(p: string): string
  export function join(...paths: string[]): string
  export function resolve(...paths: string[]): string
  export function relative(from: string, to: string): string
}

declare module 'node:url' {
  export function fileURLToPath(url: string): string
}
