/**
 * Run Audiobookshelf's OWN parsers over paths and sidecars, and print what they produced.
 *
 * The point of this file is that it contains no parsing logic. Reimplementing ABS's regexes here
 * would produce a check that agrees with itself forever and notices nothing when ABS changes
 * them. So it requires the real modules out of a real checkout and only marshals data in and out.
 *
 * Input on stdin, JSON:
 *   { "absRepo": "/path/to/audiobookshelf",
 *     "dirs": ["Author/Series/1 - Title [B0000000AA]", ...],
 *     "sidecars": [{"label": "...", "json": "<raw metadata.json text>"}] }
 *
 * Output on stdout, JSON:
 *   { "ok": true,
 *     "stubbed": ["axios", ...],
 *     "dirs":     [{ "dir":..., "title":..., "asin":..., "seriesName":..., "seriesSequence":...,
 *                    "publishedYear":..., "authors":[...], "narrators":[...] }],
 *     "sidecars": [{ "label":..., "parsed": {...}|null, "series": [{name, sequence}] }] }
 *
 * Audiobookshelf pulls a few packages in transitively that the parsing path never touches. Rather
 * than make every user install its full dependency tree, any BARE specifier that fails to resolve
 * is stubbed with an empty object and reported. Relative requires are never stubbed, so the code
 * under test is genuinely ABS's and a broken relative path still fails loudly.
 */
'use strict'

const Module = require('module')
const path = require('path')

const stubbed = new Set()
function installStubs() {
  const realLoad = Module._load
  Module._load = function (request, parent, isMain) {
    try {
      return realLoad.apply(this, arguments)
    } catch (err) {
      const bare = !request.startsWith('.') && !request.startsWith('/')
      if (err && err.code === 'MODULE_NOT_FOUND' && bare) {
        stubbed.add(request)
        return {}
      }
      throw err
    }
  }
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', (chunk) => (data += chunk))
    process.stdin.on('end', () => resolve(data))
    process.stdin.on('error', reject)
  })
}

async function main() {
  const raw = await readStdin()
  let input
  try {
    input = JSON.parse(raw)
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: `bad input JSON: ${err.message}` }))
    process.exit(2)
  }

  const absRepo = input.absRepo
  if (!absRepo) {
    process.stdout.write(JSON.stringify({ ok: false, error: 'absRepo is required' }))
    process.exit(2)
  }

  installStubs()

  let scandir, abmetadata, parseSeriesString
  try {
    scandir = require(path.join(absRepo, 'server/utils/scandir.js'))
    abmetadata = require(path.join(absRepo, 'server/utils/generators/abmetadataGenerator.js'))
    parseSeriesString = require(path.join(absRepo, 'server/utils/parsers/parseSeriesString.js'))
  } catch (err) {
    process.stdout.write(
      JSON.stringify({
        ok: false,
        error: `could not load Audiobookshelf modules from ${absRepo}: ${err.message}`
      })
    )
    process.exit(2)
  }

  const dirs = (input.dirs || []).map((dir) => {
    try {
      const r = scandir.getBookDataFromDir(dir, false)
      return {
        dir,
        title: r.title ?? null,
        subtitle: r.subtitle ?? null,
        asin: r.asin ?? null,
        seriesName: r.seriesName ?? null,
        seriesSequence: r.seriesSequence ?? null,
        publishedYear: r.publishedYear ?? null,
        authors: r.authors || [],
        narrators: r.narrators || []
      }
    } catch (err) {
      return { dir, error: err.message }
    }
  })

  const sidecars = (input.sidecars || []).map((entry) => {
    try {
      // How many series entries the file offered, before ABS had an opinion about them. The
      // interesting failure is a file that is accepted while its series are quietly discarded,
      // which is invisible unless the before and after counts are both reported.
      let offeredSeries = 0
      try {
        const rawInput = JSON.parse(entry.json)
        offeredSeries = Array.isArray(rawInput.series) ? rawInput.series.length : 0
      } catch (_) {
        offeredSeries = 0
      }

      const parsed = abmetadata.parseJson(entry.json, 'book')
      // parseJson already turns a well-formed "Name #1a" into {name, sequence}. Anything still a
      // string here has not been through that, so parse it; anything else is passed along as ABS
      // produced it rather than being reinterpreted.
      const series = (parsed && Array.isArray(parsed.series) ? parsed.series : []).map((s) =>
        typeof s === 'string' ? parseSeriesString.parse(s) : s
      )
      return {
        label: entry.label,
        parsed: parsed || null,
        series,
        offeredSeries,
        keptSeries: series.length
      }
    } catch (err) {
      return { label: entry.label, error: err.message }
    }
  })

  process.stdout.write(
    JSON.stringify({ ok: true, stubbed: [...stubbed].sort(), dirs, sidecars })
  )
}

main().catch((err) => {
  process.stdout.write(JSON.stringify({ ok: false, error: err.message }))
  process.exit(2)
})
