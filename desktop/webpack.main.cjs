module.exports = {
  entry: './src/main.ts', devtool: 'source-map',
  module: { rules: [{ test: /\.tsx?$/, exclude: /node_modules/, use: 'ts-loader' }] },
  resolve: { extensions: ['.js', '.ts', '.tsx'] },
};
