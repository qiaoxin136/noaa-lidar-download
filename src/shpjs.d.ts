declare module 'shpjs' {
  function shp(buffer: ArrayBuffer | string): Promise<any>
  export default shp
}
