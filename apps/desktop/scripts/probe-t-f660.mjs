import { CDP } from './perf/lib/cdp.mjs'

const cdp = await CDP.connect({ port: 9334, match: '5177' })
const out = await cdp.eval(`document.body.innerText.slice(0,800)`)
console.log(JSON.stringify(out))
cdp.close()
