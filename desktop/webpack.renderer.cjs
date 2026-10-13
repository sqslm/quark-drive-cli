module.exports = {
  devtool: 'source-map',
  module: { rules: [
    { test: /\.tsx?$/, exclude: /node_modules/, use: 'ts-loader' },
    { test: /\.css$/, use: ['style-loader', 'css-loader'] },
    { test: /\.png$/, type: 'asset/resource' },
  ] },
  resolve: { extensions: ['.js', '.ts', '.tsx'] },
};
