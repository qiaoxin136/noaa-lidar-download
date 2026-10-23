import { defineBackend } from '@aws-amplify/backend'
import { Duration } from 'aws-cdk-lib'
import { CorsHttpMethod, HttpApi, HttpMethod } from 'aws-cdk-lib/aws-apigatewayv2'
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations'
import { Runtime } from 'aws-cdk-lib/aws-lambda'
import { NodejsFunction } from 'aws-cdk-lib/aws-lambda-nodejs'
import { fileURLToPath } from 'node:url'

const backend = defineBackend({})
const stack = backend.createStack('noaa-lidar-proxy')

const proxyFn = new NodejsFunction(stack, 'NoaaDavProxyFn', {
  entry: fileURLToPath(new URL('./functions/noaa-proxy.ts', import.meta.url)),
  runtime: Runtime.NODEJS_22_X,
  timeout: Duration.seconds(30),
  memorySize: 512,
  bundling: { externalModules: [] },
})

const api = new HttpApi(stack, 'NoaaDavHttpApi', {
  corsPreflight: {
    allowOrigins: ['*'],
    allowMethods: [CorsHttpMethod.GET, CorsHttpMethod.POST, CorsHttpMethod.OPTIONS],
    allowHeaders: ['content-type', 'authorization'],
  },
})

api.addRoutes({
  path: '/noaa/{proxy+}',
  methods: [HttpMethod.GET, HttpMethod.POST],
  integration: new HttpLambdaIntegration('NoaaProxyIntegration', proxyFn),
})

backend.addOutput({
  custom: {
    noaa_api: api.apiEndpoint,
  },
})
