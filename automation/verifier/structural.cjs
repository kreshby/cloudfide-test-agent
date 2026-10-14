'use strict'
// Image-owned parser. Generated UTF-8 bytes are data, never loaded as a module.
const fs = require('node:fs')
const path = require('node:path').posix
const crypto = require('node:crypto')
const ts = require('/opt/frontend/node_modules/typescript')
if (ts.version !== '5.8.3') throw Error('compiler pin')
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex')
const root = '/work/repo'
const contracts = JSON.parse(fs.readFileSync(root + '/structural-input.json', 'utf8'))
const results = []
// Fixed candidate order; never glob, inspect node_modules, or load a module.
const suffixes = ['', '.ts', '.tsx', '.js', '.jsx', '.json',
  '/index.ts', '/index.tsx', '/index.js', '/index.jsx']
function snapshotFile(candidate) {
  const parts = candidate.split('/')
  if (parts[0] !== 'frontend' || parts.some(p => !p || p === '.' || p === '..' || p === 'node_modules')) return false
  let current = root
  for (let i = 0; i < parts.length; i++) {
    current += '/' + parts[i]
    try {
      const stat = fs.lstatSync(current)
      if (stat.isSymbolicLink() || (i === parts.length - 1 ? !stat.isFile() || stat.nlink !== 1 : !stat.isDirectory())) return false
    } catch { return false }
  }
  return true
}
function packageRoot(specifier) {
  const parts = specifier.split('/')
  const scoped = specifier.startsWith('@')
  if (!parts.length || parts.some(p => !p || p === '.' || p === '..') ||
      !/^(?:@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]*(?:\/[a-zA-Z0-9._-]+)*$/.test(specifier) ||
      scoped && parts.length < 2) return null
  return parts.slice(0, scoped ? 2 : 1).join('/')
}
if (!snapshotFile('frontend/package.json')) throw Error('manifest snapshot')
const manifestBytes = fs.readFileSync(root + '/frontend/package.json')
if (manifestBytes.length > 16384) throw Error('manifest bound')
const manifest = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(manifestBytes))
if (!manifest || Array.isArray(manifest) || typeof manifest !== 'object') throw Error('manifest')
const allowed = new Set()
for (const groupName of ['dependencies', 'devDependencies', 'peerDependencies']) {
  const group = manifest[groupName] === undefined ? {} : manifest[groupName]
  if (!group || Array.isArray(group) || typeof group !== 'object') throw Error('manifest group')
  for (const name of Object.keys(group)) {
    if (name.length > 128 || packageRoot(name) !== name || typeof group[name] !== 'string' || !group[name]) throw Error('manifest package')
    allowed.add(name)
  }
}
if (allowed.size > 64 || [...allowed].reduce((n, p) => n + Buffer.byteLength(p), 0) > 2048) throw Error('manifest roots bound')
for (const c of contracts) {
  const bytes = fs.readFileSync(root + '/' + c.path)
  if (bytes.length > 65536 || hash(bytes) !== c.content_sha256) throw Error('binding')
  const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes)
  const kind = c.jsx_allowed ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  const source = ts.createSourceFile(c.path, text, ts.ScriptTarget.Latest, true, kind)
  const imports = []
  let jsx = 0
  const jsxKinds = new Set(['JsxElement', 'JsxSelfClosingElement', 'JsxFragment',
    'JsxOpeningElement', 'JsxClosingElement', 'JsxOpeningFragment', 'JsxClosingFragment',
    'JsxAttribute', 'JsxAttributes', 'JsxSpreadAttribute', 'JsxExpression', 'JsxText']
    .map(name => ts.SyntaxKind[name]))
  function visit(node) {
    if (jsxKinds.has(node.kind)) jsx++
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      // All imports have safety checks; only value imports are relevance evidence.
      const clause = node.importClause
      const value = !!(clause && !clause.isTypeOnly &&
        (clause.name || clause.namedBindings &&
          (ts.isNamespaceImport(clause.namedBindings) ||
           clause.namedBindings.elements.some(e => !e.isTypeOnly))))
      imports.push({ specifier: node.moduleSpecifier.text, value })
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier &&
               ts.isStringLiteral(node.moduleSpecifier)) {
      imports.push({ specifier: node.moduleSpecifier.text, value: false })
    } else if (ts.isExternalModuleReference(node) && node.expression &&
               ts.isStringLiteral(node.expression)) {
      imports.push({ specifier: node.expression.text, value: false })
    } else if (ts.isCallExpression(node) &&
               (node.expression.kind === ts.SyntaxKind.ImportKeyword ||
                ts.isIdentifier(node.expression) && node.expression.text === 'require') &&
               node.arguments.length && ts.isStringLiteralLike(node.arguments[0])) {
      imports.push({ specifier: node.arguments[0].text, value: false })
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  const categories = []
  if (source.parseDiagnostics.length) categories.push('syntax-invalid')
  if (!c.jsx_allowed && jsx) categories.push('jsx-not-allowed')
  let subjectImported = false
  let unsafe = false
  let unresolved = false
  let unknown = false
  for (const { specifier, value } of imports.slice(0, 128)) {
    if (!specifier || specifier.length > 256 || /[\\\x00-\x1f\x7f]/.test(specifier) ||
        specifier.startsWith('/') || /^(?:[a-z]:|file:)/i.test(specifier)) {
      unsafe = true; continue
    }
    if (!specifier.startsWith('.')) {
      const packageName = packageRoot(specifier)
      if (!packageName || !allowed.has(packageName)) unknown = true
      continue
    }
    if ((!specifier.startsWith('./') && !specifier.startsWith('../')) ||
        !/^[a-zA-Z0-9._/-]+$/.test(specifier) || specifier.includes('//') ||
        specifier.split('/').includes('node_modules')) {
      unresolved = true; continue
    }
    const relative = path.normalize(path.join(path.dirname(c.path), specifier))
    const stack = path.dirname(c.path).split('/')
    let escaped = false
    for (const part of specifier.split('/')) {
      if (part === '..') {
        if (stack.length <= 1) { escaped = true; break }
        stack.pop()
      } else if (part !== '.') stack.push(part)
    }
    if (escaped || !relative.startsWith('frontend/') || relative.split('/').includes('node_modules')) {
      unresolved = true; unsafe = true; continue
    }
    const resolved = suffixes.map(suffix => relative + suffix).find(snapshotFile)
    if (!resolved) unresolved = true
    if (value && resolved === c.selected_source_path) subjectImported = true
  }
  if (imports.length > 128) unsafe = true
  if (unsafe) categories.push('unsafe-import-path')
  if (unresolved) categories.push('unresolved-relative-import')
  if (unknown) categories.push('unknown-package-import')
  const passed = categories.length === 0
  if (!subjectImported) categories.push('selected-subject-not-directly-imported')
  results.push({ ...c, validator: 'typescript-structural-v1',
    syntax_diagnostics: Math.min(source.parseDiagnostics.length, 65536),
    jsx_nodes: Math.min(jsx, 65536), subject_imported: subjectImported,
    categories, passed })
}
process.stdout.write(JSON.stringify(results))
process.exitCode = results.every(r => r.passed) ? 0 : 1
