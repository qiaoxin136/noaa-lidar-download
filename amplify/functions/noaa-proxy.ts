import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda'

const NOAA_API_BASE = 'https://coast.noaa.gov/dataviewer/api/v1'

export const handler = async (event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> => {
  const corsHeaders = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type,Authorization',
    'Content-Type': 'application/json; charset=utf-8',
  }

  const method = event.requestContext.http.method.toUpperCase()
  if (method === 'OPTIONS') {
    return { statusCode: 204, headers: corsHeaders, body: '' }
  }

  try {
    const proxyPath = event.pathParameters?.proxy || ''
    const query = event.rawQueryString ? `?${event.rawQueryString}` : ''
    const targetUrl = `${NOAA_API_BASE}/${proxyPath}${query}`

    const bodyText =
      event.body && event.isBase64Encoded
        ? Buffer.from(event.body, 'base64').toString('utf-8')
        : event.body || undefined

    const resp = await fetch(targetUrl, {
      method,
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        'User-Agent': 'Mozilla/5.0 (NoaaLidarAmplifyAgent/1.0)',
      },
      body: method === 'POST' ? bodyText : undefined,
    })

    const text = await resp.text()
    return {
      statusCode: resp.status,
      headers: corsHeaders,
      body: text,
    }
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err)
    return {
      statusCode: 500,
      headers: corsHeaders,
      body: JSON.stringify({ error: message }),
    }
  }
}
