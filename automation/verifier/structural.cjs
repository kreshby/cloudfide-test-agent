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
  for (const { specifier, value } of imports) {
    if (!specifier || /[\\\x00-\x1f\x7f]/.test(specifier) ||
        specifier.startsWith('/') || /^(?:[a-z]:|file:)/i.test(specifier)) {
      unsafe = true; continue
    }
    if (!specifier.startsWith('./') && !specifier.startsWith('../')) continue
    const relative = path.normalize(path.join(path.dirname(c.path), specifier))
    if (relative.startsWith('../') || relative.startsWith('/') || relative === '..') {
      unsafe = true; continue
    }
    if (!value) continue
    const candidates = /\.tsx?$/.test(relative) ? [relative] :
      [relative + '.ts', relative + '.tsx', relative + '/index.ts', relative + '/index.tsx']
    // Bounded relevance heuristic only. The compiler owns module correctness.
    const matches = candidates.filter(p => {
      try { return fs.lstatSync(root + '/' + p).isFile() } catch { return false }
    })
    if (matches.length !== 1) continue
    if (matches[0] === c.selected_source_path) subjectImported = true
  }
  if (unsafe) categories.push('unsafe-import-path')
  const passed = categories.length === 0
  if (!subjectImported) categories.push('selected-subject-not-directly-imported')
  results.push({ ...c, validator: 'typescript-structural-v1',
    syntax_diagnostics: Math.min(source.parseDiagnostics.length, 65536),
    jsx_nodes: Math.min(jsx, 65536), subject_imported: subjectImported,
    categories, passed })
}
process.stdout.write(JSON.stringify(results))
process.exitCode = results.every(r => r.passed) ? 0 : 1
